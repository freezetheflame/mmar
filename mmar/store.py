from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import os
import queue
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .context import build_context
from .protocol import Decision, Limits, MmarError, StepSnapshot, bounded_text, dumps, identifier


class BudgetExceeded(MmarError):
    def __init__(self, scope: str):
        self.scope = scope
        super().__init__("budget_exceeded")


def uid() -> str:
    return uuid.uuid4().hex


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS control (
 id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL, limits_json TEXT NOT NULL,
 steps INTEGER NOT NULL DEFAULT 0, calls INTEGER NOT NULL DEFAULT 0,
 deliveries INTEGER NOT NULL DEFAULT 0, budget_epoch INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS agents (
 id TEXT PRIMARY KEY, state TEXT NOT NULL, identity_ref INTEGER, memory_ref INTEGER,
 directory TEXT, exported_ref INTEGER, export_retry REAL NOT NULL DEFAULT 0, steps INTEGER NOT NULL DEFAULT 0,
 calls INTEGER NOT NULL DEFAULT 0, blocked INTEGER NOT NULL DEFAULT 0,
 turn INTEGER NOT NULL DEFAULT 0, last_session TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS texts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL REFERENCES agents(id),
 kind TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, state TEXT NOT NULL, epoch INTEGER NOT NULL DEFAULT 0,
 steps INTEGER NOT NULL DEFAULT 0, calls INTEGER NOT NULL DEFAULT 0,
 deliveries INTEGER NOT NULL DEFAULT 0, reason TEXT);
CREATE TABLE IF NOT EXISTS participants (
 session_id TEXT NOT NULL REFERENCES sessions(id), agent_id TEXT NOT NULL REFERENCES agents(id),
 PRIMARY KEY(session_id,agent_id));
CREATE TABLE IF NOT EXISTS groups_ (
 session_id TEXT NOT NULL REFERENCES sessions(id), id TEXT NOT NULL,
 members TEXT NOT NULL, senders TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(session_id,id));
CREATE TABLE IF NOT EXISTS messages (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), sender TEXT NOT NULL,
 kind TEXT NOT NULL, content TEXT NOT NULL, target_json TEXT NOT NULL, created_at TEXT NOT NULL,
 step_id TEXT, action_index INTEGER, source TEXT, request_id TEXT, payload_hash TEXT,
 UNIQUE(step_id,action_index), UNIQUE(source,request_id));
CREATE TABLE IF NOT EXISTS steps (
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id),
 session_id TEXT NOT NULL REFERENCES sessions(id), epoch INTEGER NOT NULL,
 identity_ref INTEGER NOT NULL REFERENCES texts(id), memory_ref INTEGER NOT NULL REFERENCES texts(id),
 state TEXT NOT NULL, manifest TEXT NOT NULL, decision TEXT, error TEXT, created_at TEXT NOT NULL,
 finished_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_running_step ON steps(agent_id) WHERE state='running';
CREATE INDEX IF NOT EXISTS session_steps_idx ON steps(session_id,state);
CREATE TABLE IF NOT EXISTS deliveries (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL REFERENCES messages(id),
 agent_id TEXT NOT NULL REFERENCES agents(id), state TEXT NOT NULL,
 step_id TEXT REFERENCES steps(id), recoveries INTEGER NOT NULL DEFAULT 0, reason TEXT,
 UNIQUE(message_id,agent_id));
CREATE INDEX IF NOT EXISTS inbox_idx ON deliveries(agent_id,state,seq);
CREATE INDEX IF NOT EXISTS pending_idx ON deliveries(state,seq);
CREATE INDEX IF NOT EXISTS message_session_idx ON messages(session_id);
CREATE INDEX IF NOT EXISTS agent_turn_idx ON agents(turn);
CREATE TABLE IF NOT EXISTS attempts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, step_id TEXT NOT NULL REFERENCES steps(id),
 state TEXT NOT NULL, usage TEXT, error TEXT, started_at TEXT NOT NULL, finished_at TEXT);
CREATE TABLE IF NOT EXISTS events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, session_id TEXT, agent_id TEXT,
 step_id TEXT, data TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS budget_history (
 epoch INTEGER PRIMARY KEY, counters TEXT NOT NULL, created_at TEXT NOT NULL);
PRAGMA user_version=1;
"""


class Store:
    """One connection owned by one thread. Every command has a transaction boundary."""

    def __init__(self, path: Path, root: Path, limits: Limits):
        self.path, self.root, self.limits = path.resolve(), root.resolve(), limits
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = open(str(self.path) + ".lock", "a+b")
        try:
            self._lock.seek(0)
            if os.name == "nt":
                import msvcrt
                if self._lock.read(1) == b"":
                    self._lock.write(b"0")
                    self._lock.flush()
                self._lock.seek(0)
                msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock.close()
            raise MmarError("store_in_use") from None
        self._queue = queue.Queue(maxsize=limits.store_queue)
        self._ready = concurrent.futures.Future()
        self._closed = False
        self._closing = False
        self._shutdown_task = None
        self._thread = threading.Thread(target=self._worker, name="mmar-store", daemon=True)
        self._thread.start()

    async def ready(self):
        try:
            await asyncio.shield(asyncio.wrap_future(self._ready))
        except asyncio.CancelledError:
            try:
                await asyncio.shield(asyncio.wrap_future(self._ready))
            except Exception:
                await asyncio.to_thread(self._thread.join)
                self._unlock()
            else:
                await self.close()
            raise
        except BaseException:
            await asyncio.to_thread(self._thread.join)
            self._unlock()
            raise

    def _unlock(self):
        if self._lock.closed:
            return
        if os.name == "nt":
            import msvcrt
            self._lock.seek(0)
            msvcrt.locking(self._lock.fileno(), msvcrt.LK_UNLCK, 1)
        self._lock.close()

    def _worker(self):
        try:
            self.db = sqlite3.connect(self.path, timeout=1, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA journal_mode=DELETE")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise MmarError("unsupported_schema")
            self.db.executescript(SCHEMA)
            config = dumps(asdict(self.limits))
            row = self.db.execute("SELECT * FROM control").fetchone()
            if row and row["limits_json"] != config:
                raise MmarError("limits_mismatch")
            self.db.execute("INSERT OR IGNORE INTO control(id,state,limits_json) VALUES(1,'CREATED',?)", (config,))
            self._execute("recover", {})
            self._ready.set_result(None)
        except BaseException as error:
            if hasattr(self, "db"):
                self.db.close()
            self._ready.set_exception(error)
            return
        try:
            while True:
                name, kwargs, future = self._queue.get()
                if name == "close":
                    future.set_result(None)
                    break
                try:
                    future.set_result(self._execute(name, kwargs))
                except BaseException as error:
                    future.set_exception(error)
        finally:
            self.db.close()

    def _execute(self, name, kwargs):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            result = getattr(self, "_" + name)(**kwargs)
            self.db.commit()
            return result
        except BudgetExceeded as error:
            self.db.rollback()
            self.db.execute("BEGIN IMMEDIATE")
            self._drain(error.scope)
            if name in ("inject", "commit"):
                self._event("message.rejected", reason=error.code, method=name)
            self._settle()
            self.db.commit()
            raise
        except MmarError as error:
            self.db.rollback()
            if name in ("inject", "commit"):
                self.db.execute("BEGIN IMMEDIATE")
                self._event("message.rejected", reason=error.code, method=name)
                self.db.commit()
            raise
        except BaseException:
            self.db.rollback()
            raise

    async def call(self, name, _wait=False, **kwargs):
        future = concurrent.futures.Future()
        while True:
            if self._closed or (self._closing and name != "close"):
                raise MmarError("store_closed")
            try:
                self._queue.put_nowait((name, kwargs, future))
                break
            except queue.Full:
                if not _wait:
                    raise MmarError("runtime_busy") from None
                await asyncio.sleep(0.005)
        # A cancelled caller does not cancel an already admitted DB command.
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        return await asyncio.shield(wrapped)

    async def close(self):
        if self._closed:
            return
        if self._shutdown_task is None:
            self._closing = True
            self._shutdown_task = asyncio.create_task(self._shutdown(), name="mmar-store-close")
        await asyncio.shield(self._shutdown_task)

    async def _shutdown(self):
        await self.call("close", _wait=True)
        self._closed = True
        await asyncio.to_thread(self._thread.join)
        self._unlock()

    def _one(self, sql, params=()):
        return self.db.execute(sql, params).fetchone()

    def _event(self, kind, session=None, agent=None, step=None, **data):
        self.db.execute("INSERT INTO events(kind,session_id,agent_id,step_id,data,created_at) VALUES(?,?,?,?,?,?)",
                        (kind, session, agent, step, dumps(data), now()))

    def _agent(self, agent_id):
        row = self._one("SELECT * FROM agents WHERE id=?", (agent_id,))
        if not row or row["state"] != "active":
            raise MmarError("agent_unavailable")
        return row

    def _session(self, session_id):
        row = self._one("SELECT * FROM sessions WHERE id=?", (session_id,))
        if not row:
            raise MmarError("unknown_session")
        return row

    def _admin(self):
        if self._one("SELECT state FROM control")[0] not in ("CREATED", "PAUSED"):
            raise MmarError("pause_required")

    def _text(self, ref):
        return self._one("SELECT body FROM texts WHERE id=?", (ref,))[0]

    def _revision(self, agent, kind, body):
        return self.db.execute("INSERT INTO texts(agent_id,kind,body) VALUES(?,?,?)", (agent, kind, body)).lastrowid

    def _read_file(self, path, limit):
        path = path.resolve()
        if not path.is_relative_to(self.root):
            raise MmarError("path_outside_agent_root")
        with path.open("rb") as stream:
            body = stream.read(limit + 1)
        if len(body) > limit:
            raise MmarError("text_too_large")
        return body.decode("utf-8-sig")

    def _register(self, agent_id, identity=None, memory="# Memory", directory=None):
        identifier(agent_id)
        if self._one("SELECT state FROM control")[0] not in ("CREATED", "RUNNING", "PAUSED"):
            raise MmarError("runtime_not_accepting")
        if directory is not None:
            directory = Path(directory).resolve()
            if not directory.is_relative_to(self.root):
                raise MmarError("path_outside_agent_root")
            identity = self._read_file(directory / "agent.md", self.limits.identity_bytes)
            if (directory / "memory.md").exists():
                memory = self._read_file(directory / "memory.md", self.limits.memory_bytes)
        bounded_text(identity, self.limits.identity_bytes)
        bounded_text(memory, self.limits.memory_bytes)
        existing = self._one("SELECT * FROM agents WHERE id=?", (agent_id,))
        if existing:
            if existing["state"] != "active":
                raise MmarError("agent_retired")
            if self._text(existing["identity_ref"]) != identity or existing["directory"] != (str(directory) if directory else None):
                raise MmarError("registration_conflict")
            return agent_id
        self.db.execute("INSERT INTO agents(id,state,directory) VALUES(?,'active',?)", (agent_id, str(directory) if directory else None))
        ident = self._revision(agent_id, "identity", identity)
        mem = self._revision(agent_id, "memory", memory)
        self.db.execute("UPDATE agents SET identity_ref=?,memory_ref=? WHERE id=?", (ident, mem, agent_id))
        self._event("agent.registered", agent=agent_id)
        return agent_id

    def _directory(self, session, agent):
        peers = [r[0] for r in self.db.execute("SELECT p.agent_id FROM participants p JOIN agents a ON a.id=p.agent_id WHERE p.session_id=? AND p.agent_id!=? AND a.state='active' ORDER BY p.agent_id", (session, agent))]
        groups = []
        for row in self.db.execute("SELECT * FROM groups_ WHERE session_id=? ORDER BY id", (session,)):
            if agent in json.loads(row["senders"]):
                groups.append({"id": row["id"], "can_send": True,
                               "recipient_count": len(set(json.loads(row["members"])) - {agent})})
        result = {"self": agent, "peers": peers, "groups": groups}
        bounded_text(dumps(result), self.limits.directory_bytes, "directory_too_large")
        return result

    def _create_session(self, session_id, participants):
        self._admin()
        identifier(session_id)
        if not participants or len(set(participants)) != len(participants):
            raise MmarError("invalid_participants")
        for agent in participants:
            self._agent(identifier(agent))
        if self._one("SELECT 1 FROM sessions WHERE id=?", (session_id,)):
            raise MmarError("session_exists")
        self.db.execute("INSERT INTO sessions(id,state) VALUES(?,'OPEN')", (session_id,))
        self.db.executemany("INSERT INTO participants VALUES(?,?)", [(session_id, agent) for agent in participants])
        for agent in participants:
            self._directory(session_id, agent)
        self._event("session.started", session=session_id)
        return session_id

    def _create_group(self, session_id, group_id, members, senders=None):
        self._admin()
        identifier(group_id)
        if self._session(session_id)["state"] not in ("OPEN", "IDLE"):
            raise MmarError("session_closed")
        senders = members if senders is None else senders
        allowed = {r[0] for r in self.db.execute("SELECT agent_id FROM participants WHERE session_id=?", (session_id,))}
        if not members or len(set(members)) != len(members) or len(set(senders)) != len(senders) or not set(members + senders) <= allowed:
            raise MmarError("invalid_group")
        if self._one("SELECT 1 FROM groups_ WHERE session_id=? AND id=?", (session_id, group_id)):
            raise MmarError("group_exists")
        self.db.execute("INSERT INTO groups_(session_id,id,members,senders) VALUES(?,?,?,?)",
                        (session_id, group_id, dumps(members), dumps(senders)))
        for agent in allowed:
            self._directory(session_id, agent)
        return group_id

    def _set_participants(self, session_id, participants):
        self._admin()
        if self._session(session_id)["state"] not in ("OPEN", "IDLE"):
            raise MmarError("session_closed")
        if not participants or len(set(participants)) != len(participants):
            raise MmarError("invalid_participants")
        for agent in participants:
            self._agent(identifier(agent))
        old = {r[0] for r in self.db.execute("SELECT agent_id FROM participants WHERE session_id=?", (session_id,))}
        for agent in old - set(participants):
            self.db.execute("UPDATE deliveries SET state='cancelled',reason='participant_removed' WHERE agent_id=? AND state='pending' AND message_id IN(SELECT id FROM messages WHERE session_id=?)", (agent, session_id))
        self.db.execute("DELETE FROM participants WHERE session_id=?", (session_id,))
        self.db.executemany("INSERT INTO participants VALUES(?,?)", [(session_id, a) for a in participants])
        for group in self.db.execute("SELECT * FROM groups_ WHERE session_id=?", (session_id,)).fetchall():
            members = [a for a in json.loads(group["members"]) if a in participants]
            senders = [a for a in json.loads(group["senders"]) if a in participants]
            self.db.execute("UPDATE groups_ SET members=?,senders=?,revision=revision+1 WHERE session_id=? AND id=?", (dumps(members), dumps(senders), session_id, group["id"]))
        for agent in participants:
            self._directory(session_id, agent)
        self._event("session.participants_updated", session=session_id)

    def _capacity(self, targets):
        total = self._one("SELECT count(*) FROM deliveries WHERE state IN ('pending','claimed')")[0]
        if total + len(targets) > self.limits.pending_total:
            raise MmarError("queue_full")
        for target in set(targets):
            count = self._one("SELECT count(*) FROM deliveries WHERE agent_id=? AND state IN ('pending','claimed')", (target,))[0]
            if count + targets.count(target) > self.limits.inbox:
                raise MmarError("inbox_full")

    def _spend(self, session, agent=None, metric="calls", amount=1):
        config = self.limits
        global_row = self._one("SELECT * FROM control")
        if global_row[metric] + amount > getattr(config, metric):
            raise BudgetExceeded("global")
        session_row = self._session(session)
        if session_row[metric] + amount > getattr(config, "session_" + metric):
            raise BudgetExceeded("session:" + session)
        if agent is not None and metric in ("steps", "calls"):
            agent_row = self._agent(agent)
            if agent_row[metric] + amount > getattr(config, "agent_" + metric):
                raise BudgetExceeded("agent:" + agent)
            self.db.execute(f"UPDATE agents SET {metric}={metric}+? WHERE id=?", (amount, agent))
        self.db.execute(f"UPDATE sessions SET {metric}={metric}+? WHERE id=?", (amount, session))
        self.db.execute(f"UPDATE control SET {metric}={metric}+?", (amount,))

    def _message(self, session, sender, kind, content, targets, target_spec,
                 step=None, index=None, source=None, request=None, digest=None):
        message = uid()
        self.db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (message, session, sender, kind, content, dumps(target_spec), now(), step, index, source, request, digest))
        self.db.executemany("INSERT INTO deliveries(message_id,agent_id,state) VALUES(?,?,'pending')", [(message, target) for target in targets])
        self.db.execute("UPDATE sessions SET state='OPEN' WHERE id=? AND state='IDLE'", (session,))
        self._event("message.accepted", session=session, agent=sender, step=step, message_id=message, recipients=targets)
        return message

    def _inject(self, session_id, target, content, request_id, source="environment", kind="external"):
        identifier(session_id)
        identifier(source)
        identifier(request_id)
        identifier(target)
        bounded_text(content, self.limits.message_bytes)
        if kind not in ("external", "activation"):
            raise MmarError("invalid_input_kind")
        digest = hashlib.sha256(dumps([session_id, target, content, kind]).encode()).hexdigest()
        previous = self._one("SELECT id,payload_hash FROM messages WHERE source=? AND request_id=?", (source, request_id))
        if previous:
            if previous["payload_hash"] != digest:
                raise MmarError("idempotency_conflict")
            return previous["id"]
        if self._one("SELECT state FROM control")[0] not in ("CREATED", "RUNNING", "PAUSED", "PAUSING"):
            raise MmarError("runtime_not_accepting")
        session = self._session(session_id)
        if session["state"] not in ("OPEN", "IDLE"):
            raise MmarError("session_closed")
        self._recipient(session_id, target)
        global_row = self._one("SELECT * FROM control")
        agent_row = self._agent(target)
        for metric in ("steps", "calls"):
            if global_row[metric] >= getattr(self.limits, metric):
                raise BudgetExceeded("global")
            if session[metric] >= getattr(self.limits, "session_" + metric):
                raise BudgetExceeded("session:" + session_id)
            if agent_row[metric] >= getattr(self.limits, "agent_" + metric):
                raise BudgetExceeded("agent:" + target)
        self._capacity([target])
        self._spend(session_id, metric="deliveries")
        return self._message(session_id, "env:" + source, kind, content, [target], [target], source=source, request=request_id, digest=digest)

    def _recipient(self, session, target):
        agent = self._agent(target)
        if agent["blocked"]:
            raise MmarError("agent_budget_blocked")
        if not self._one("SELECT 1 FROM participants WHERE session_id=? AND agent_id=?", (session, target)):
            raise MmarError("target_outside_session")

    def _claim(self, excluded):
        self._settle()
        if self._one("SELECT state FROM control")[0] != "RUNNING":
            return None
        excluded_clause = "AND a.id NOT IN (" + ",".join("?" for _ in excluded) + ")" if excluded else ""
        row = self._one(f"""SELECT a.* FROM agents a WHERE a.state='active' AND a.blocked=0
            {excluded_clause} AND NOT EXISTS (SELECT 1 FROM steps st WHERE st.agent_id=a.id AND st.state='running')
            AND EXISTS (SELECT 1 FROM deliveries d JOIN messages m ON m.id=d.message_id JOIN sessions s ON s.id=m.session_id
             WHERE d.agent_id=a.id AND d.state='pending' AND s.state IN ('OPEN','IDLE'))
            ORDER BY a.turn,a.id LIMIT 1""", tuple(excluded))
        if not row:
            return None
        agent = row["id"]
        session_ids = [r[0] for r in self.db.execute("SELECT DISTINCT m.session_id FROM deliveries d JOIN messages m ON m.id=d.message_id JOIN sessions s ON s.id=m.session_id WHERE d.agent_id=? AND d.state='pending' AND s.state IN ('OPEN','IDLE') ORDER BY m.session_id", (agent,))]
        session_id = next((s for s in session_ids if s > row["last_session"]), session_ids[0])
        session = self._session(session_id)
        directory = self._directory(session_id, agent)
        identity, memory = self._text(row["identity_ref"]), self._text(row["memory_ref"])
        limits = {"actions": self.limits.actions, "targets": self.limits.targets,
                  "fanout": self.limits.fanout, "message_bytes": self.limits.message_bytes,
                  "memory_bytes": self.limits.memory_bytes}
        candidates = self.db.execute("SELECT d.seq,m.id AS message_id,m.sender,m.kind,m.content FROM deliveries d JOIN messages m ON m.id=d.message_id WHERE d.agent_id=? AND m.session_id=? AND d.state='pending' ORDER BY d.seq LIMIT ?", (agent, session_id, self.limits.batch_size)).fetchall()
        step_id = uid()
        selected = []
        def snapshot(history=()):
            return StepSnapshot(step_id, agent, session_id, identity, memory, directory, tuple(selected), tuple(history), limits)
        for message in candidates:
            selected.append(dict(message))
            if len(dumps(build_context(snapshot(), repair=True)).encode()) > self.limits.context_bytes:
                selected.pop()
                if not selected:
                    self.db.execute("UPDATE deliveries SET state='failed',reason='context_overflow' WHERE seq=?", (message["seq"],))
                    self._event("delivery.failed", session=session_id, agent=agent, reason="context_overflow", delivery_seq=message["seq"])
                break
        turn = self._one("SELECT coalesce(max(turn),0)+1 FROM agents")[0]
        self.db.execute("UPDATE agents SET turn=?,last_session=? WHERE id=?", (turn, session_id, agent))
        if not selected:
            return None
        history = []
        if self.limits.history_messages:
            rows = self.db.execute("""SELECT m.id AS message_id,m.sender,m.kind,m.content FROM messages m
                WHERE m.session_id=? AND (m.sender=? OR EXISTS(SELECT 1 FROM deliveries d WHERE d.message_id=m.id AND d.agent_id=? AND d.state='consumed'))
                ORDER BY m.rowid DESC LIMIT ?""", (session_id, agent, agent, self.limits.history_messages)).fetchall()
            for item in reversed(rows):
                history.append(dict(item))
                if len(dumps(build_context(snapshot(history), repair=True)).encode()) > self.limits.context_bytes:
                    history.pop()
        self._spend(session_id, agent, "steps")
        manifest = {"directory": directory, "deliveries": [m["seq"] for m in selected], "history": [m["message_id"] for m in history]}
        self.db.execute("INSERT INTO steps(id,agent_id,session_id,epoch,identity_ref,memory_ref,state,manifest,created_at) VALUES(?,?,?,?,?,?,'running',?,?)",
                        (step_id, agent, session_id, session["epoch"], row["identity_ref"], row["memory_ref"], dumps(manifest), now()))
        self.db.executemany("UPDATE deliveries SET state='claimed',step_id=? WHERE seq=?", [(step_id, m["seq"]) for m in selected])
        self._event("step.claimed", session=session_id, agent=agent, step=step_id, delivery_seqs=manifest["deliveries"])
        return snapshot(history)

    def _valid_step(self, step_id):
        step = self._one("SELECT * FROM steps WHERE id=?", (step_id,))
        if not step or step["state"] != "running":
            raise MmarError("stale_step")
        session = self._session(step["session_id"])
        agent = self._agent(step["agent_id"])
        if session["state"] not in ("OPEN", "IDLE", "DRAINING") or session["epoch"] != step["epoch"]:
            raise MmarError("stale_step")
        if (agent["identity_ref"], agent["memory_ref"]) != (step["identity_ref"], step["memory_ref"]):
            raise MmarError("version_conflict")
        return step

    def _reserve(self, step_id):
        step = self._valid_step(step_id)
        attempts = self._one("SELECT count(*) FROM attempts WHERE step_id=?", (step_id,))[0]
        if attempts >= self.limits.attempts:
            raise MmarError("attempt_limit")
        self._spend(step["session_id"], step["agent_id"], "calls")
        attempt = self.db.execute("INSERT INTO attempts(step_id,state,started_at) VALUES(?,'started',?)", (step_id, now())).lastrowid
        self._event("attempt.started", session=step["session_id"], agent=step["agent_id"], step=step_id, attempt_id=attempt)
        return attempt

    def _finish_attempt(self, attempt_id, usage=None, error=None):
        usage = {k: v for k, v in (usage or {}).items() if k in ("prompt_tokens", "completion_tokens", "total_tokens") and type(v) is int and v >= 0} or None
        self.db.execute("UPDATE attempts SET state=?,usage=?,error=?,finished_at=? WHERE id=? AND state='started'",
                        ("failed" if error else "finished", dumps(usage) if usage else None, error, now(), attempt_id))
        step = self._one("SELECT step_id FROM attempts WHERE id=?", (attempt_id,))
        self._event("attempt.finished", step=step[0], attempt_id=attempt_id, error=error, usage=usage)

    def _commit(self, step_id, decision):
        previous = self._one("SELECT state FROM steps WHERE id=?", (step_id,))
        if previous and previous[0] == "committed":
            return step_id
        step = self._valid_step(step_id)
        decision = Decision.parse(decision, self.limits)
        session, sender = step["session_id"], step["agent_id"]
        expanded = []
        all_targets = []
        for action in decision.actions:
            if action["type"] == "send":
                targets = action["targets"]
                kind = "direct" if len(targets) == 1 else "multicast"
                spec = targets
            else:
                group = self._one("SELECT * FROM groups_ WHERE session_id=? AND id=?", (session, action["group"]))
                if not group or sender not in json.loads(group["senders"]):
                    raise MmarError("group_not_allowed")
                targets = sorted(set(json.loads(group["members"])) - {sender})
                kind, spec = "group", {"group": group["id"], "revision": group["revision"]}
            if not targets or sender in targets:
                raise MmarError("invalid_targets")
            for target in targets:
                self._recipient(session, target)
            all_targets.extend(targets)
            expanded.append((action, kind, targets, spec))
        if len(all_targets) > self.limits.fanout:
            raise MmarError("fanout_exceeded")
        self._capacity(all_targets)
        if all_targets:
            self._spend(session, metric="deliveries", amount=len(all_targets))
        if decision.memory_replace is not None:
            ref = self._revision(sender, "memory", decision.memory_replace)
            self.db.execute("UPDATE agents SET memory_ref=? WHERE id=?", (ref, sender))
            self._event("memory.updated", session=session, agent=sender, step=step_id, revision=ref)
        for index, (action, kind, targets, spec) in enumerate(expanded):
            self._message(session, sender, kind, action["content"], targets, spec, step_id, index)
        self.db.execute("UPDATE deliveries SET state='consumed' WHERE step_id=? AND state='claimed'", (step_id,))
        self.db.execute("UPDATE steps SET state='committed',decision=?,finished_at=? WHERE id=?", (dumps(decision.to_dict()), now(), step_id))
        self._event("step.committed", session=session, agent=sender, step=step_id)
        self._settle()
        return step_id

    def _fail(self, step_id, reason, state="failed"):
        step = self._one("SELECT * FROM steps WHERE id=? AND state='running'", (step_id,))
        if step:
            self.db.execute("UPDATE steps SET state=?,error=?,finished_at=? WHERE id=?", (state, reason, now(), step_id))
            self.db.execute("UPDATE deliveries SET state=?,reason=? WHERE step_id=? AND state='claimed'", (state, reason, step_id))
            self.db.execute("UPDATE attempts SET state='unknown',error=?,finished_at=? WHERE step_id=? AND state='started'", (reason, now(), step_id))
            self._event("step." + state, session=step["session_id"], agent=step["agent_id"], step=step_id, reason=reason)
        self._settle()

    def _drain(self, scope):
        if scope == "global":
            self.db.execute("UPDATE control SET state='DRAINING'")
            self.db.execute("UPDATE sessions SET state='DRAINING',reason='global_budget' WHERE state IN ('OPEN','IDLE')")
        elif scope.startswith("session:"):
            self.db.execute("UPDATE sessions SET state='DRAINING',reason='session_budget' WHERE id=? AND state IN ('OPEN','IDLE')", (scope[8:],))
        else:
            agent = scope[6:]
            self.db.execute("UPDATE agents SET blocked=1 WHERE id=?", (agent,))
            self.db.execute("UPDATE deliveries SET state='cancelled',reason='agent_budget' WHERE agent_id=? AND state='pending'", (agent,))
        self._event("session.budget_exceeded", scope=scope)

    def _settle(self):
        for session in self.db.execute("SELECT * FROM sessions WHERE state IN ('OPEN','IDLE','DRAINING')").fetchall():
            running = self._one("SELECT count(*) FROM steps WHERE session_id=? AND state='running'", (session["id"],))[0]
            if session["state"] == "DRAINING" and not running:
                self.db.execute("UPDATE deliveries SET state='cancelled',reason=? WHERE state IN ('pending','claimed') AND message_id IN (SELECT id FROM messages WHERE session_id=?)", (session["reason"], session["id"]))
                state = "STOPPED" if session["reason"] == "stop" else "BUDGET_EXCEEDED"
                self.db.execute("UPDATE sessions SET state=? WHERE id=?", (state, session["id"]))
                self._event("session.stopped", session=session["id"], reason=session["reason"])
            elif session["state"] == "OPEN" and not running:
                count = self._one("SELECT count(*) FROM deliveries d JOIN messages m ON m.id=d.message_id WHERE m.session_id=? AND d.state IN ('pending','claimed')", (session["id"],))[0]
                if not count:
                    self.db.execute("UPDATE sessions SET state='IDLE' WHERE id=?", (session["id"],))
                    self._event("session.idle", session=session["id"])
        state = self._one("SELECT state FROM control")[0]
        if state in ("DRAINING", "STOPPING") and not self._one("SELECT 1 FROM steps WHERE state='running'"):
            self.db.execute("UPDATE control SET state=?", ("BUDGET_EXCEEDED" if state == "DRAINING" else "STOPPED",))

    def _control(self, state):
        old = self._one("SELECT state FROM control")[0]
        allowed = {"RUNNING": ("CREATED", "PAUSED"), "PAUSING": ("RUNNING", "PAUSING"), "PAUSED": ("PAUSING", "PAUSED")}
        if state not in allowed or old not in allowed[state]:
            raise MmarError("invalid_runtime_transition")
        if state == "PAUSED" and self._one("SELECT 1 FROM steps WHERE state='running'"):
            raise MmarError("steps_still_running")
        self.db.execute("UPDATE control SET state=?", (state,))
        return state

    def _stop(self, session_id=None, mode="cancel"):
        ids = [session_id] if session_id else [r[0] for r in self.db.execute("SELECT id FROM sessions WHERE state IN ('OPEN','IDLE','DRAINING')")]
        if not session_id:
            self.db.execute("UPDATE control SET state='STOPPING'")
        for session in ids:
            row = self._session(session)
            if row["state"] in ("STOPPED", "BUDGET_EXCEEDED"):
                continue
            if mode == "drain":
                self.db.execute("UPDATE sessions SET state='DRAINING',reason='stop' WHERE id=?", (session,))
            else:
                self.db.execute("UPDATE sessions SET state='STOPPED',epoch=epoch+1,reason='stop' WHERE id=?", (session,))
                for step in self.db.execute("SELECT id FROM steps WHERE session_id=? AND state='running'", (session,)).fetchall():
                    self._fail(step[0], "stop", "cancelled")
                self.db.execute("UPDATE deliveries SET state='cancelled',reason='stop' WHERE state='pending' AND message_id IN (SELECT id FROM messages WHERE session_id=?)", (session,))
                self._event("session.stopped", session=session, reason="stop")
        self._settle()

    def _retire(self, agent_id):
        agent = self._one("SELECT * FROM agents WHERE id=?", (agent_id,))
        if not agent:
            raise MmarError("agent_unavailable")
        if agent["state"] == "retired":
            return
        self.db.execute("UPDATE agents SET state='retired' WHERE id=?", (agent_id,))
        for step in self.db.execute("SELECT id FROM steps WHERE agent_id=? AND state='running'", (agent_id,)).fetchall():
            self._fail(step[0], "retired", "cancelled")
        self.db.execute("UPDATE deliveries SET state='cancelled',reason='retired' WHERE agent_id=? AND state='pending'", (agent_id,))
        self.db.execute("DELETE FROM participants WHERE agent_id=?", (agent_id,))
        for group in self.db.execute("SELECT * FROM groups_").fetchall():
            members = [a for a in json.loads(group["members"]) if a != agent_id]
            senders = [a for a in json.loads(group["senders"]) if a != agent_id]
            self.db.execute("UPDATE groups_ SET members=?,senders=?,revision=revision+1 WHERE session_id=? AND id=?", (dumps(members), dumps(senders), group["session_id"], group["id"]))
        self._event("agent.retired", agent=agent_id)
        self._settle()

    def _recover(self):
        self.db.execute("UPDATE attempts SET state='unknown',error='interrupted',finished_at=? WHERE state='started'", (now(),))
        for step in self.db.execute("SELECT * FROM steps WHERE state='running'").fetchall():
            self.db.execute("UPDATE steps SET state='interrupted',error='process_interrupted',finished_at=? WHERE id=?", (now(), step["id"]))
            session = self._session(step["session_id"])
            agent = self._one("SELECT * FROM agents WHERE id=?", (step["agent_id"],))
            for delivery in self.db.execute("SELECT * FROM deliveries WHERE step_id=? AND state='claimed'", (step["id"],)).fetchall():
                terminal = session["state"] not in ("OPEN", "IDLE") or agent["state"] != "active" or agent["blocked"]
                state = "cancelled" if terminal else "pending" if delivery["recoveries"] < self.limits.recovery_attempts else "failed"
                self.db.execute("UPDATE deliveries SET state=?,recoveries=recoveries+1,reason='process_interrupted' WHERE seq=?", (state, delivery["seq"]))
            self._event("step.interrupted", session=step["session_id"], agent=step["agent_id"], step=step["id"])
        # Explicit management stop survives a process crash before cleanup.
        if self._one("SELECT state FROM control")[0] == "STOPPING":
            self._stop()
        self._settle()
        self.db.execute("UPDATE control SET state='CREATED'")

    def _state(self, session_id=None, agent_id=None, metrics=False):
        self._settle()
        if agent_id:
            row = self._one("SELECT * FROM agents WHERE id=?", (agent_id,))
            if not row:
                raise MmarError("agent_unavailable")
            result = dict(row)
            result["memory"] = self._text(row["memory_ref"])
            result["pending"] = self._one("SELECT count(*) FROM deliveries WHERE agent_id=? AND state IN ('pending','claimed')", (agent_id,))[0]
            return result
        result = dict(self._session(session_id) if session_id else self._one("SELECT * FROM control"))
        result.pop("limits_json", None)
        where = " AND m.session_id=?" if session_id else ""
        args = (session_id,) if session_id else ()
        result["pending"] = self._one("SELECT count(*) FROM deliveries d JOIN messages m ON m.id=d.message_id WHERE d.state IN ('pending','claimed')" + where, args)[0]
        result["active"] = self._one("SELECT count(*) FROM steps WHERE state='running'" + (" AND session_id=?" if session_id else ""), args)[0]
        result["event_seq"] = self._one("SELECT coalesce(max(seq),0) FROM events")[0]
        result["step_states"] = {r[0]: r[1] for r in self.db.execute("SELECT state,count(*) FROM steps" + (" WHERE session_id=?" if session_id else "") + " GROUP BY state", args)}
        if metrics and not session_id:
            result["agents"] = self._one("SELECT count(*) FROM agents WHERE state='active'")[0]
            result["sqlite_version"] = sqlite3.sqlite_version
            result["usage_known_attempts"] = self._one("SELECT count(*) FROM attempts WHERE usage IS NOT NULL")[0]
            result["usage_unknown_attempts"] = self._one("SELECT count(*) FROM attempts WHERE usage IS NULL AND state!='started'")[0]
            result["reported_tokens"] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            for record in self.db.execute("SELECT usage FROM attempts WHERE usage IS NOT NULL"):
                for key, value in json.loads(record[0]).items():
                    result["reported_tokens"][key] += value
        return result

    def _events(self, after=0, limit=100):
        return [{**dict(r), "data": json.loads(r["data"])} for r in self.db.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, min(max(limit, 1), 1000)))]

    def _get_message(self, message_id):
        row = self._one("SELECT * FROM messages WHERE id=?", (message_id,))
        if not row:
            raise MmarError("unknown_message")
        result = dict(row)
        result["target"] = json.loads(result.pop("target_json"))
        result["deliveries"] = [dict(r) for r in self.db.execute("SELECT seq,agent_id,state,reason FROM deliveries WHERE message_id=? ORDER BY seq", (message_id,))]
        return result

    def _exports(self, limit=8):
        return [{"agent": r["id"], "directory": r["directory"], "revision": r["memory_ref"], "body": self._text(r["memory_ref"])} for r in self.db.execute("SELECT * FROM agents WHERE directory IS NOT NULL AND export_retry<=? AND (exported_ref IS NULL OR exported_ref!=memory_ref) LIMIT ?", (time.time(), limit))]

    def _export_failed(self, agent, revision):
        self.db.execute("UPDATE agents SET export_retry=? WHERE id=?", (time.time() + 5, agent))
        self._event("memory.export_failed", agent=agent, revision=revision)

    def _export_status(self):
        return [dict(r) for r in self.db.execute("SELECT id,memory_ref,exported_ref FROM agents WHERE directory IS NOT NULL AND (exported_ref IS NULL OR memory_ref!=exported_ref)")]

    def _exported(self, agent, revision):
        self.db.execute("UPDATE agents SET exported_ref=? WHERE id=? AND (exported_ref IS NULL OR exported_ref<?)", (revision, agent, revision))

    def _edit(self, agent_id, kind, text=None, expected_revision=None):
        self._admin()
        row = self._agent(agent_id)
        if kind not in ("identity", "memory"):
            raise MmarError("invalid_text_kind")
        ref_key = kind + "_ref"
        if expected_revision is not None and row[ref_key] != expected_revision:
            raise MmarError("version_conflict")
        if text is None:
            if not row["directory"]:
                raise MmarError("agent_has_no_directory")
            text = self._read_file(Path(row["directory"]) / ("agent.md" if kind == "identity" else "memory.md"), getattr(self.limits, kind + "_bytes"))
        bounded_text(text, getattr(self.limits, kind + "_bytes"))
        ref = self._revision(agent_id, kind, text)
        self.db.execute(f"UPDATE agents SET {ref_key}=? WHERE id=?", (ref, agent_id))
        self._event(kind + ".updated", agent=agent_id, revision=ref)
        return ref

    def _reset_budget(self):
        self._admin()
        if self._one("SELECT 1 FROM steps WHERE state='running'"):
            raise MmarError("steps_still_running")
        row = dict(self._one("SELECT * FROM control"))
        row.pop("limits_json")
        self.db.execute("INSERT INTO budget_history VALUES(?,?,?)", (row["budget_epoch"], dumps(row), now()))
        self.db.execute("UPDATE control SET steps=0,calls=0,deliveries=0,budget_epoch=budget_epoch+1")
        self.db.execute("UPDATE agents SET steps=0,calls=0,blocked=0")
        self._event("budget.reset")
