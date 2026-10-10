import asyncio
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from mmar import Decision, Limits, MmarError, MockModel, ModelReply, Runtime
from mmar.protocol import InvalidDecision, TransientModelError


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.db"
        self.runtimes = []

    async def asyncTearDown(self):
        for runtime in reversed(self.runtimes):
            if not runtime._closed:
                await runtime.close()
        self.temp.cleanup()

    async def runtime(self, model=None, limits=None, agents=("a", "b", "c")):
        runtime = await Runtime.open(self.path, model=model or MockModel(), limits=limits)
        self.runtimes.append(runtime)
        for agent in agents:
            await runtime.register_agent(agent, identity="Identity: " + agent)
        await runtime.create_session("s", list(agents))
        return runtime

    def rows(self, sql, params=()):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(sql, params).fetchall()

    async def seed(self, r, target="a", key="seed", session="s"):
        return await r.inject_message(session, target, "hello", key)

    async def test_silence_consumes_and_idle(self):
        r = await self.runtime()
        await self.seed(r)
        await r.start()
        result = await r.wait_idle("s")
        self.assertEqual(result["step_states"], {"committed": 1})
        self.assertEqual(self.rows("SELECT state FROM deliveries"), [("consumed",)])

    async def test_multi_target_broadcast_and_memory(self):
        def policy(s):
            if s.agent_id == "a" and s.messages[0]["kind"] == "external":
                return Decision(({"type": "send", "targets": ["b", "c"], "content": "question"},), "# Memory\n- asked")
            if s.agent_id == "b" and s.messages[0]["kind"] == "multicast":
                return Decision(({"type": "broadcast", "group": "room", "content": "answer"},))
            return Decision()
        r = await self.runtime(MockModel(policy))
        await r.create_group("s", "room", ["a", "b", "c"])
        await self.seed(r)
        await r.start()
        await r.wait_idle("s")
        self.assertEqual(self.rows("SELECT kind FROM messages ORDER BY rowid"), [("external",), ("multicast",), ("group",)])
        self.assertEqual(self.rows("SELECT count(*) FROM deliveries"), [(5,)])
        self.assertEqual((await r.read_memory("a"))["content"], "# Memory\n- asked")
        self.assertFalse(self.rows("SELECT 1 FROM deliveries d JOIN messages m ON m.id=d.message_id WHERE m.kind='group' AND d.agent_id='b'"))

    async def test_invalid_target_rolls_back_whole_decision(self):
        r = await self.runtime(MockModel(lambda _: Decision((
            {"type": "send", "targets": ["b"], "content": "valid"},
            {"type": "send", "targets": ["missing"], "content": "invalid"}), "changed")))
        await self.seed(r)
        await r.start()
        result = await r.wait_idle("s")
        self.assertEqual(result["step_states"], {"failed": 1})
        self.assertEqual((await r.read_memory("a"))["content"], "# Memory")
        self.assertEqual(self.rows("SELECT count(*) FROM messages"), [(1,)])

    async def test_inbox_overflow_rolls_back(self):
        limits = replace(Limits(), concurrency=1, inbox=1)
        r = await self.runtime(MockModel(lambda _: Decision(({"type": "send", "targets": ["b"], "content": "x"},), "changed")), limits)
        await self.seed(r)
        await self.seed(r, "b", "second")
        await r.start()
        await r.wait_idle("s")
        self.assertEqual(self.rows("SELECT count(*) FROM messages WHERE step_id IS NOT NULL"), [(0,)])
        self.assertEqual((await r.read_memory("a"))["content"], "# Memory")

    async def test_fanout_is_deliveries_not_actions(self):
        limits = replace(Limits(), fanout=1)
        r = await self.runtime(MockModel(lambda _: Decision(({"type": "broadcast", "group": "room", "content": "x"},))), limits)
        await r.create_group("s", "room", ["a", "b", "c"])
        await self.seed(r)
        await r.start()
        await r.wait_idle("s")
        self.assertEqual(self.rows("SELECT error FROM steps"), [("fanout_exceeded",)])

    async def test_concurrency_and_agent_serialization_across_sessions(self):
        counts, maximum, global_count = {}, {}, [0, 0]
        overlap = asyncio.Event()
        async def policy(s):
            counts[s.agent_id] = counts.get(s.agent_id, 0) + 1
            maximum[s.agent_id] = max(maximum.get(s.agent_id, 0), counts[s.agent_id])
            global_count[0] += 1
            global_count[1] = max(global_count)
            if global_count[0] == 2:
                overlap.set()
            await asyncio.wait_for(overlap.wait(), 5)
            await asyncio.sleep(0.025)
            global_count[0] -= 1
            counts[s.agent_id] -= 1
            return Decision()
        r = await self.runtime(MockModel(policy, 0), replace(Limits(), concurrency=2, batch_size=1))
        await r.create_session("t", ["a", "b", "c"])
        for session in ("s", "t"):
            for index in range(4):
                for agent in ("a", "b", "c"):
                    await self.seed(r, agent, session + agent + str(index), session)
        await r.start()
        await r.wait_idle()
        self.assertEqual(global_count[1], 2)
        self.assertTrue(all(v == 1 for v in maximum.values()))
        self.assertEqual(self.rows("SELECT count(*) FROM steps"), [(24,)])

    async def test_message_arriving_during_step_not_lost(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def policy(s):
            entered.set()
            await release.wait()
            return Decision()
        r = await self.runtime(MockModel(policy, 0), replace(Limits(), batch_size=1))
        await self.seed(r)
        await r.start()
        await asyncio.wait_for(entered.wait(), 1)
        await self.seed(r, key="later")
        release.set()
        await r.wait_idle()
        self.assertEqual(self.rows("SELECT count(*) FROM steps WHERE state='committed'"), [(2,)])

    async def test_calls_budget_includes_retries(self):
        def policy(_):
            raise TransientModelError("transient")
        r = await self.runtime(MockModel(policy), replace(Limits(), calls=1, retry_delay=0))
        await self.seed(r)
        await r.start()
        await r.wait_idle()
        self.assertEqual((await r.metrics())["calls"], 1)
        self.assertEqual(self.rows("SELECT count(*) FROM attempts"), [(1,)])
        self.assertEqual((await r.get_session_state("s"))["state"], "BUDGET_EXCEEDED")

    async def test_concurrent_calls_cannot_overdraw(self):
        r = await self.runtime(limits=replace(Limits(), calls=1))
        for agent in ("a", "b", "c"):
            await self.seed(r, agent, agent)
        await r.start()
        await r.wait_idle()
        self.assertEqual((await r.metrics())["calls"], 1)
        self.assertEqual(self.rows("SELECT count(*) FROM attempts"), [(1,)])

    async def test_format_repair_once_and_counted(self):
        class Repair(MockModel):
            async def complete(self, snapshot, *, repair=False):
                return ModelReply(Decision() if repair else "not json")
        r = await self.runtime(Repair())
        await self.seed(r)
        await r.start()
        await r.wait_idle()
        self.assertEqual((await r.metrics())["calls"], 2)
        self.assertEqual(self.rows("SELECT state FROM steps"), [("committed",)])

    async def test_bad_model_does_not_cancel_other_agents(self):
        def policy(s):
            return "invalid" if s.agent_id == "a" else Decision()
        r = await self.runtime(MockModel(policy))
        await self.seed(r)
        await self.seed(r, "b", "b")
        await r.start()
        await r.wait_idle()
        self.assertEqual(self.rows("SELECT agent_id,state FROM steps ORDER BY agent_id"), [("a", "failed"), ("b", "committed")])

    async def test_pause_allows_commit_and_preserves_inbox(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def policy(_):
            entered.set()
            await release.wait()
            return Decision(memory_replace="paused memory")
        r = await self.runtime(MockModel(policy, 0))
        await self.seed(r)
        await r.start()
        await entered.wait()
        pause = asyncio.create_task(r.pause())
        await asyncio.sleep(0.02)
        self.assertFalse(pause.done())
        release.set()
        await pause
        self.assertEqual(r.state, "PAUSED")
        await self.seed(r, key="paused")
        await asyncio.sleep(0.02)
        self.assertEqual((await r.metrics())["calls"], 1)
        await r.resume()
        await r.wait_idle()
        self.assertEqual((await r.metrics())["calls"], 2)

    async def test_stop_discards_late_response(self):
        entered = asyncio.Event()
        class Late(MockModel):
            async def complete(self, snapshot, *, repair=False):
                entered.set()
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    return ModelReply(Decision(memory_replace="must not commit"))
        r = await self.runtime(Late())
        await self.seed(r)
        await r.start()
        await entered.wait()
        await r.stop()
        self.assertEqual((await r.read_memory("a"))["content"], "# Memory")
        self.assertEqual(self.rows("SELECT state FROM steps"), [("cancelled",)])
        self.assertEqual(r.state, "STOPPED")

    async def test_drain_finishes_only_inflight(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def policy(_):
            entered.set()
            await release.wait()
            return Decision(memory_replace="drained")
        r = await self.runtime(MockModel(policy, 0), replace(Limits(), concurrency=1, batch_size=1))
        await self.seed(r)
        await self.seed(r, key="second")
        await r.start()
        await entered.wait()
        stop = asyncio.create_task(r.stop(mode="drain"))
        await asyncio.sleep(0.02)
        release.set()
        await stop
        self.assertEqual((await r.metrics())["calls"], 1)
        self.assertEqual(self.rows("SELECT state FROM deliveries ORDER BY seq"), [("consumed",), ("cancelled",)])

    async def test_unregister_tombstone(self):
        entered = asyncio.Event()
        async def policy(_):
            entered.set()
            await asyncio.sleep(10)
        r = await self.runtime(MockModel(policy, 0))
        await self.seed(r)
        await r.start()
        await entered.wait()
        await r.unregister_agent("a")
        self.assertEqual((await r.get_agent_state("a"))["state"], "retired")
        with self.assertRaisesRegex(MmarError, "agent_retired"):
            await r.register_agent("a", identity="new")

    async def test_input_idempotency_and_conflict(self):
        r = await self.runtime()
        first = await self.seed(r)
        self.assertEqual(await self.seed(r), first)
        with self.assertRaisesRegex(MmarError, "idempotency_conflict"):
            await r.inject_message("s", "a", "different", "seed")
        self.assertEqual(self.rows("SELECT count(*) FROM messages"), [(1,)])

    async def test_commit_idempotency(self):
        r = await self.runtime()
        await self.seed(r)
        await r._store.call("control", state="RUNNING")
        snap = await r._store.call("claim", excluded=[])
        decision = Decision(({"type": "send", "targets": ["b"], "content": "once"},), "remember")
        await r._store.call("commit", step_id=snap.step_id, decision=decision)
        await r._store.call("commit", step_id=snap.step_id, decision=decision)
        self.assertEqual(self.rows("SELECT count(*) FROM messages"), [(2,)])

    async def test_group_permission_and_session_boundaries(self):
        r = await self.runtime(MockModel(lambda _: Decision(({"type": "broadcast", "group": "room", "content": "x"},))))
        await r.create_group("s", "room", ["a", "b"], ["b"])
        await self.seed(r)
        await r.start()
        await r.wait_idle()
        self.assertEqual(self.rows("SELECT error FROM steps"), [("group_not_allowed",)])
        await r.pause()
        await r.create_session("t", ["b"])
        with self.assertRaisesRegex(MmarError, "target_outside_session"):
            await r.inject_message("t", "a", "x", "outside")

    async def test_single_owner_and_persistent_budget(self):
        limits = replace(Limits(), calls=9)
        r = await self.runtime(limits=limits)
        with self.assertRaisesRegex(MmarError, "store_in_use"):
            await Runtime.open(self.path, model=MockModel(), limits=limits)
        await self.seed(r)
        await r.start()
        await r.wait_idle()
        await r.close()
        reopened = await Runtime.open(self.path, model=MockModel(), limits=limits)
        self.runtimes.append(reopened)
        self.assertEqual((await reopened.metrics())["calls"], 1)
        self.assertEqual(self.rows("SELECT count(*) FROM messages"), [(1,)])

    async def test_memory_export_and_revision_conflict(self):
        root = Path(self.temp.name) / "agents"
        directory = root / "a"
        directory.mkdir(parents=True)
        (directory / "agent.md").write_text("Identity", encoding="utf-8")
        r = await Runtime.open(self.path, model=MockModel(lambda _: Decision(memory_replace="# Updated")), agent_root=root)
        self.runtimes.append(r)
        await r.register_agent("a", directory=directory)
        await r.create_session("s", ["a"])
        old = (await r.read_memory("a"))["revision"]
        await self.seed(r)
        await r.start()
        await r.wait_idle()
        self.assertEqual(await r.flush_exports(), [])
        self.assertEqual((directory / "memory.md").read_text(), "# Updated")
        await r.pause()
        with self.assertRaisesRegex(MmarError, "version_conflict"):
            await r.import_memory("a", "manual", expected_revision=old)

    async def test_no_tasks_per_idle_agent(self):
        r = await self.runtime(agents=tuple(f"a{i}" for i in range(100)))
        await r.start()
        await asyncio.sleep(0.02)
        owned = [t for t in asyncio.all_tasks() if t.get_name().startswith("mmar-")]
        self.assertEqual(len(owned), 2)
        self.assertEqual((await r.metrics())["task_count"], 0)

    async def test_events_resume_from_cursor(self):
        r = await self.runtime()
        first = await r.read_events()
        cursor = first[-1]["seq"]
        await self.seed(r)
        iterator = r.events(cursor)
        event = await asyncio.wait_for(anext(iterator), 1)
        self.assertGreater(event["seq"], cursor)
        self.assertEqual(event["kind"], "message.accepted")
        await iterator.aclose()

    async def test_event_output_can_be_read_by_environment(self):
        r = await self.runtime()
        message_id = await self.seed(r)
        message = await r.read_message(message_id)
        self.assertEqual(message["content"], "hello")
        self.assertEqual(message["deliveries"][0]["agent_id"], "a")

    async def test_export_failure_does_not_resend(self):
        root = Path(self.temp.name) / "agents"
        directory = root / "a"
        directory.mkdir(parents=True)
        (directory / "agent.md").write_text("a", encoding="utf-8")
        r = await Runtime.open(self.path, model=MockModel(lambda _: Decision(memory_replace="updated")), agent_root=root)
        self.runtimes.append(r)
        await r.register_agent("a", directory=directory)
        await r.create_session("s", ["a"])
        def denied(_):
            raise PermissionError()
        r._write_export = denied
        await self.seed(r)
        await r.start()
        await r.wait_idle()
        self.assertTrue(await r.flush_exports())
        self.assertEqual((await r.read_memory("a"))["content"], "updated")
        self.assertEqual((await r.metrics())["calls"], 1)
        self.assertEqual(self.rows("SELECT state FROM steps"), [("committed",)])

    async def test_path_escape_rejected(self):
        r = await self.runtime()
        with self.assertRaisesRegex(MmarError, "path_outside_agent_root"):
            await r.register_agent("evil", directory=Path(self.temp.name))
        with self.assertRaisesRegex(MmarError, "invalid_identifier"):
            await r.register_agent("../other", identity="evil")

    async def test_store_queue_is_bounded(self):
        r = await self.runtime(limits=replace(Limits(), store_queue=2))
        entered, release = threading.Event(), threading.Event()
        def block():
            entered.set()
            release.wait(3)
        r._store._block = block
        blocked = asyncio.create_task(r._store.call("block"))
        await asyncio.to_thread(entered.wait, 1)
        first = asyncio.create_task(r.read_events())
        second = asyncio.create_task(r.read_events())
        await asyncio.sleep(0.01)
        try:
            with self.assertRaisesRegex(MmarError, "runtime_busy"):
                await r.read_events()
        finally:
            release.set()
            await asyncio.gather(blocked, first, second)

    async def test_internal_store_work_waits_under_backpressure(self):
        r = await self.runtime(limits=replace(Limits(), store_queue=1))
        entered, release = threading.Event(), threading.Event()
        def block():
            entered.set()
            release.wait(3)
        r._store._block = block
        blocked = asyncio.create_task(r._store.call("block"))
        await asyncio.to_thread(entered.wait, 1)
        queued = asyncio.create_task(r.read_events())
        await asyncio.sleep(0.01)
        internal = asyncio.create_task(r._work("state"))
        await asyncio.sleep(0.01)
        self.assertFalse(internal.done())
        release.set()
        await asyncio.gather(blocked, queued, internal)
        self.assertIsNone(r._fault)

    async def test_cancelling_admitted_input_does_not_undo_commit(self):
        r = await self.runtime()
        entered, release = threading.Event(), threading.Event()
        def block():
            entered.set()
            release.wait(3)
        r._store._block = block
        blocked = asyncio.create_task(r._store.call("block"))
        await asyncio.to_thread(entered.wait, 1)
        injection = asyncio.create_task(self.seed(r))
        await asyncio.sleep(0.01)
        injection.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await injection
        release.set()
        await blocked
        self.assertEqual((await r.metrics())["pending"], 1)
        message = await self.seed(r)
        self.assertEqual(message, self.rows("SELECT id FROM messages")[0][0])

    async def test_model_close_error_does_not_leak_store_lock(self):
        class BadClose(MockModel):
            async def aclose(self):
                raise RuntimeError("close failed")
        r = await self.runtime(BadClose())
        with self.assertRaisesRegex(RuntimeError, "close failed"):
            await r.close()
        self.assertTrue(r._closed)
        reopened = await Runtime.open(self.path, model=MockModel())
        self.runtimes.append(reopened)

    async def test_request_timeout_and_attempt_limit(self):
        r = await self.runtime(MockModel(delay=1), replace(Limits(), request_timeout=0.01, retry_delay=0, attempts=2))
        await self.seed(r)
        await r.start()
        await r.wait_idle()
        self.assertEqual((await r.metrics())["calls"], 2)
        self.assertEqual(self.rows("SELECT error FROM steps"), [("request_timeout",)])

    async def test_group_recipient_total_budget_terminates_session(self):
        r = await self.runtime(MockModel(lambda _: Decision(({"type": "broadcast", "group": "room", "content": "x"},), "not saved")),
                               replace(Limits(), session_deliveries=2))
        await r.create_group("s", "room", ["a", "b", "c"])
        await self.seed(r)
        await r.start()
        await r.wait_idle()
        self.assertEqual((await r.get_session_state("s"))["state"], "BUDGET_EXCEEDED")
        self.assertEqual((await r.read_memory("a"))["content"], "# Memory")
        self.assertEqual((await r.get_session_state("s"))["deliveries"], 1)

    async def test_session_stop_does_not_cancel_other_session(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def policy(snapshot):
            if snapshot.session_id == "s":
                entered.set()
                await release.wait()
            return Decision()
        r = await self.runtime(MockModel(policy, 0))
        await r.create_session("t", ["b"])
        await self.seed(r)
        await self.seed(r, "b", "second", "t")
        await r.start()
        await entered.wait()
        await r.stop_session("s")
        await r.wait_idle("t")
        self.assertEqual(self.rows("SELECT session_id,state FROM steps ORDER BY session_id"), [("s", "cancelled"), ("t", "committed")])

    async def test_idle_does_not_wait_for_slow_subscriber(self):
        r = await self.runtime()
        await self.seed(r)
        await r.start()
        iterator = r.events()
        await anext(iterator)
        await r.wait_idle()
        self.assertEqual((await r.metrics())["calls"], 1)
        await iterator.aclose()

    async def test_dynamic_registration_and_directory_change(self):
        r = await self.runtime()
        await r.start()
        await r.register_agent("d", identity="new")
        with self.assertRaisesRegex(MmarError, "pause_required"):
            await r.set_participants("s", ["a", "d"])
        await r.pause()
        await r.set_participants("s", ["a", "d"])
        await self.seed(r, "d")
        await r.resume()
        await r.wait_idle()
        self.assertEqual(self.rows("SELECT agent_id FROM steps"), [("d",)])

    async def test_usage_unknown_is_not_zero_cost_claim(self):
        r = await self.runtime(MockModel(lambda _: ModelReply(Decision(), {"total_tokens": 10, "secret": "not stored"})))
        await self.seed(r)
        await r.start()
        await r.wait_idle()
        metrics = await r.metrics()
        self.assertEqual(metrics["reported_tokens"]["total_tokens"], 10)
        self.assertEqual(metrics["usage_known_attempts"], 1)
        self.assertNotIn("secret", self.rows("SELECT usage FROM attempts")[0][0])

    async def test_exhausted_store_can_explicitly_reset_after_reopen(self):
        limits = replace(Limits(), calls=1)
        r = await self.runtime(limits=limits)
        await self.seed(r)
        await r.start()
        await r.wait_idle()
        with self.assertRaisesRegex(MmarError, "budget_exceeded"):
            await self.seed(r, key="over-budget")
        await r.close()
        reopened = await Runtime.open(self.path, model=MockModel(), limits=limits)
        self.runtimes.append(reopened)
        self.assertEqual((await reopened.metrics())["calls"], 1)
        await reopened.reset_budget()
        self.assertEqual((await reopened.metrics())["calls"], 0)
        self.assertEqual(self.rows("SELECT count(*) FROM budget_history"), [(1,)])
        await reopened.create_session("new", ["a"])
        await reopened.inject_message("new", "a", "new experiment", "new-input")
        await reopened.start()
        await reopened.wait_idle()
        self.assertEqual((await reopened.metrics())["calls"], 1)
        self.assertEqual((await reopened.get_session_state("s"))["state"], "BUDGET_EXCEEDED")

    async def test_rejected_message_event_has_no_input_content(self):
        r = await self.runtime()
        with self.assertRaises(MmarError):
            await r.inject_message("s", "missing", "do not copy this into errors", "bad-input")
        events = await r.read_events()
        rejected = [event for event in events if event["kind"] == "message.rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["data"]["reason"], "agent_unavailable")
        self.assertNotIn("do not copy", json.dumps(rejected))

    async def test_interrupted_commit_rolls_back_memory_and_messages(self):
        script = r'''
import asyncio,os,sys
from mmar import Runtime,MockModel,Decision
async def main():
 r=await Runtime.open(sys.argv[1],model=MockModel())
 for a in ["a","b"]: await r.register_agent(a,identity=a)
 await r.create_session("s",["a","b"])
 await r.inject_message("s","a","hello","seed")
 await r._store.call("control",state="RUNNING")
 snap=await r._store.call("claim",excluded=[])
 await r._store.call("reserve",step_id=snap.step_id)
 r._store._message=lambda *args,**kwargs: os._exit(0)
 await r._store.call("commit",step_id=snap.step_id,decision=Decision(({"type":"send","targets":["b"],"content":"must rollback"},),"must rollback"))
asyncio.run(main())
'''
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script, str(self.path)], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        r = await Runtime.open(self.path, model=MockModel())
        self.runtimes.append(r)
        self.assertEqual((await r.read_memory("a"))["content"], "# Memory")
        self.assertEqual(self.rows("SELECT count(*) FROM messages"), [(1,)])
        self.assertEqual((await r.metrics())["deliveries"], 1)
        self.assertEqual((await r.metrics())["calls"], 1)
        self.assertEqual((await r.metrics())["pending"], 1)

    async def test_recovery_retries_have_persistent_limit(self):
        script = r'''
import asyncio,os,sys
from mmar import Runtime,MockModel
async def main():
 r=await Runtime.open(sys.argv[1],model=MockModel())
 if not (await r.metrics())["agents"]:
  await r.register_agent("a",identity="a")
  await r.create_session("s",["a"])
  await r.inject_message("s","a","hello","seed")
 await r._store.call("control",state="RUNNING")
 snap=await r._store.call("claim",excluded=[])
 await r._store.call("reserve",step_id=snap.step_id)
 os._exit(0)
asyncio.run(main())
'''
        for _ in range(2):
            result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script, str(self.path)], capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
        r = await Runtime.open(self.path, model=MockModel())
        self.runtimes.append(r)
        self.assertEqual((await r.metrics())["calls"], 2)
        self.assertEqual(self.rows("SELECT state,recoveries FROM deliveries"), [("failed", 2)])
        await r.start()
        await r.wait_idle()
        self.assertEqual((await r.metrics())["calls"], 2)

    async def test_crash_after_claim_and_commit(self):
        script = r'''
import asyncio, os, sys
from mmar import Runtime, MockModel, Decision
async def main():
 r=await Runtime.open(sys.argv[1], model=MockModel())
 if not (await r.metrics())["agents"]:
  await r.register_agent("a",identity="a")
  await r.create_session("s",["a"])
  await r.inject_message("s","a","hello","seed")
 await r._store.call("control",state="RUNNING")
 snap=await r._store.call("claim",excluded=[])
 await r._store.call("reserve",step_id=snap.step_id)
 if sys.argv[2]=="commit":
  await r._store.call("commit",step_id=snap.step_id,decision=Decision(memory_replace="committed"))
 os._exit(0)
asyncio.run(main())
'''
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script, str(self.path), "claim"], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        r = await Runtime.open(self.path, model=MockModel())
        self.runtimes.append(r)
        self.assertEqual((await r.metrics())["calls"], 1)
        self.assertEqual((await r.metrics())["pending"], 1)
        await r._store.close()  # simulate owner disappearance without administrative stop
        r._closed = True
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script, str(self.path), "commit"], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        final = await Runtime.open(self.path, model=MockModel())
        self.runtimes.append(final)
        self.assertEqual((await final.read_memory("a"))["content"], "committed")
        self.assertEqual((await final.metrics())["calls"], 2)
        self.assertEqual((await final.metrics())["pending"], 0)
        await final.start()
        await final.wait_idle()
        self.assertEqual((await final.metrics())["calls"], 2)


class ProtocolTests(unittest.TestCase):
    def test_reject_unknown_fields_duplicates_and_commands(self):
        for value in ('{"actions":[],"memory_replace":null,"sender":"system"}',
                      '{"actions":[],"actions":[],"memory_replace":null}',
                      '{"actions":[{"type":"shell","content":"x"}],"memory_replace":null}',
                      '{"actions":[],"memory_replace":""}'):
            with self.assertRaises(InvalidDecision):
                Decision.parse(value, Limits())

    def test_reject_nonfinite_limits(self):
        for value in (float("nan"), float("inf"), -1):
            with self.assertRaises(ValueError):
                Limits(request_timeout=value)


if __name__ == "__main__":
    unittest.main()
