from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import tomllib
import uuid
from pathlib import Path

from .model import MockModel
from .protocol import Decision, Limits, MmarError
from .runtime import Runtime


def demo_policy(snapshot):
    if any(m["kind"] in ("external", "activation") for m in snapshot.messages):
        peers = snapshot.directory["peers"][:2]
        return Decision(({"type": "send", "targets": peers, "content": "请向讨论组分享一个看法。"},)) if peers else Decision()
    if any(m["kind"] in ("direct", "multicast") for m in snapshot.messages):
        return Decision(({"type": "broadcast", "group": "room", "content": f"{snapshot.agent_id}: 我建议先从小规模实验开始。"},), "# Memory\n\n- 已参与讨论并分享建议。")
    return Decision(memory_replace="# Memory\n\n- 听取了其他参与者的建议。")


def make_model(args, limits):
    if args.api:
        from .http_model import ChatCompletionsModel
        return ChatCompletionsModel.from_env(max_connections=limits.concurrency,
            timeout=limits.request_timeout, max_output_tokens=args.output_tokens,
            requests_per_second=args.rate)
    return MockModel(demo_policy)


async def run(args):
    if args.command == "demo":
        limits = Limits(concurrency=args.concurrency, calls=args.calls,
                        session_calls=args.calls, agent_calls=args.calls)
        root = Path(args.data).resolve()
        db = root / ("demo-" + uuid.uuid4().hex[:8] + ".db")
        runtime = await Runtime.open(db, model=make_model(args, limits), limits=limits)
        participants = [f"agent{i:03}" for i in range(args.agents)]
        try:
            for agent in participants:
                await runtime.register_agent(agent, identity=f"你是研究员 {agent}。讨论时给出简短具体的观点，可以向任意同伴或讨论组发言，也可以保持沉默。")
            session = "discussion"
            await runtime.create_session(session, participants)
            await runtime.create_group(session, "room", participants)
            await runtime.inject_message(session, participants[0], args.topic, "seed")
            await execute(runtime, session, args.timeout)
        finally:
            await runtime.close()
        print("Store:", db)
    else:
        config_path = Path(args.config).resolve()
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        limits = Limits(**config.get("limits", {}))
        root = (config_path.parent / config.get("agent_root", "agents")).resolve()
        db = (config_path.parent / config.get("store", ".mmar/discussion.db")).resolve()
        runtime = await Runtime.open(db, model=make_model(args, limits), limits=limits, agent_root=root)
        session = "run-" + uuid.uuid4().hex[:12]
        try:
            participants = []
            for agent in config["agents"]:
                participants.append(agent["id"])
                await runtime.register_agent(agent["id"], directory=root / agent["directory"])
            await runtime.create_session(session, participants)
            for group in config.get("groups", []):
                await runtime.create_group(session, group["id"], group["members"], group.get("senders"))
            seed = config["seed"]
            await runtime.inject_message(session, seed["target"], seed["content"], session + "-seed")
            await execute(runtime, session, args.timeout)
            stale = await runtime.flush_exports()
            if stale:
                print("Memory exports pending:", json.dumps(stale))
        finally:
            await runtime.close()
        print("Store:", db)


async def execute(runtime, session, timeout):
    await runtime.start()
    try:
        stats = await runtime.wait_idle(session, timeout)
    except TimeoutError:
        await runtime.stop(timeout=10)
        stats = await runtime.get_session_state(session)
        print("Discussion stopped: wall-clock timeout")
    cursor = 0
    while batch := await runtime.read_events(cursor):
        for event in batch:
            if event["kind"] == "message.accepted":
                message = await runtime.read_message(event["data"]["message_id"])
                targets = ", ".join(d["agent_id"] for d in message["deliveries"])
                print(f"[{message['sender']} -> {targets}] {message['content']}")
            cursor = event["seq"]
    print(json.dumps(stats, ensure_ascii=False, indent=2))


def main():
    # Windows redirected streams otherwise default to a legacy encoding.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="mmar", description="Minimal multi-agent runtime")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Run an offline Mock discussion, or --api")
    demo.add_argument("--agents", type=int, default=10)
    demo.add_argument("--data", default=".mmar")
    demo.add_argument("--concurrency", type=int, default=4)
    demo.add_argument("--calls", type=int, default=30)
    demo.add_argument("--topic", default="请讨论多 Agent 系统如何在保持自主性的同时节省资源。")
    configured = commands.add_parser("run", help="Run a TOML-defined discussion")
    configured.add_argument("config")
    for command in (demo, configured):
        command.add_argument("--api", action="store_true", help="Use MMAR_API_KEY / MMAR_MODEL / MMAR_API_BASE")
        command.add_argument("--timeout", type=float, default=120)
        command.add_argument("--output-tokens", type=int, default=1024)
        command.add_argument("--rate", type=float, default=1, help="Maximum model requests per second")
    inspect_parser = commands.add_parser("inspect", help="Read counters and transcript without inference")
    inspect_parser.add_argument("store")
    args = parser.parse_args()
    if args.command == "demo" and not 1 <= args.agents <= 1000:
        parser.error("agents must be between 1 and 1000")
    try:
        if args.command == "inspect":
            db = sqlite3.connect(Path(args.store).resolve().as_uri() + "?mode=ro", uri=True)
            db.row_factory = sqlite3.Row
            try:
                control = dict(db.execute("SELECT * FROM control").fetchone())
                control.pop("limits_json", None)
                print(json.dumps(control, indent=2))
                for row in db.execute("SELECT sender,kind,content,session_id FROM messages ORDER BY rowid"):
                    print(json.dumps(dict(row), ensure_ascii=False))
            finally:
                db.close()
        else:
            asyncio.run(run(args))
    except (MmarError, ValueError, OSError, ImportError, KeyError) as error:
        parser.exit(1, f"MMAR error: {error.code if isinstance(error, MmarError) else type(error).__name__}\n")

