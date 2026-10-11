from __future__ import annotations

import asyncio
import math
import os
import tempfile
from pathlib import Path

from .model import ModelAdapter
from .protocol import Decision, InvalidDecision, Limits, MmarError, TransientModelError
from .store import BudgetExceeded, Store


def _observe_task(task):
    if not task.cancelled():
        task.exception()


async def _close_adapter(model, timeout):
    task = asyncio.create_task(model.aclose(), name="mmar-model-close")
    task.add_done_callback(_observe_task)
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        task.cancel()
        await asyncio.sleep(0)
        raise MmarError("model_close_timeout")
    return task.result()


async def _cleanup_open_failure(store, model):
    try:
        if store is not None:
            try:
                await store.ready()
            except BaseException:
                # ready() joins and unlocks a failed initialization.
                pass
            else:
                await store.close()
    finally:
        await _close_adapter(model, 10)


class Runtime:
    def __init__(self, store: Store, model: ModelAdapter, limits: Limits):
        self._store, self.model, self.limits = store, model, limits
        self._state = "CREATED"
        self._active = {}
        self._scheduler = None
        self._exporter = None
        self._changed = asyncio.Event()
        self._export_lock = asyncio.Lock()
        self._dispatch_lock = asyncio.Lock()
        self._fault = None
        self._closing = False
        self._closed = False
        self._shutdown_task = None
        self._export_batch_task = None

    @classmethod
    async def open(cls, store_path, *, model: ModelAdapter, limits=None, agent_root=None):
        store = None
        try:
            limits = limits or Limits()
            path = Path(store_path)
            root = Path(agent_root) if agent_root else path.parent / "agents"
            store = Store(path, root, limits)
            await store.ready()
            return cls(store, model, limits)
        except BaseException as original:
            cleanup = asyncio.create_task(_cleanup_open_failure(store, model), name="mmar-open-cleanup")
            cleanup.add_done_callback(_observe_task)
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # A repeated caller cancellation must not cancel cleanup.
                if not cleanup.cancelled():
                    raise
                original.add_note("Initialization cleanup failed: CancelledError")
            except BaseException as error:
                original.add_note("Initialization cleanup failed: " + type(error).__name__)
            raise

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    @property
    def state(self):
        return self._state

    def _notify(self):
        event = self._changed
        self._changed = asyncio.Event()
        event.set()

    async def _work(self, method, **kwargs):
        return await self._store.call(method, _wait=True, **kwargs)

    async def _call(self, method, **kwargs):
        if self._closed:
            raise MmarError("runtime_closed")
        if self._closing:
            raise MmarError("runtime_closing")
        try:
            return await self._store.call(method, **kwargs)
        finally:
            self._notify()

    async def register_agent(self, agent_id, *, directory=None, identity=None, memory="# Memory"):
        return await self._call("register", agent_id=agent_id, directory=directory, identity=identity, memory=memory)

    async def create_session(self, session_id, participants):
        return await self._call("create_session", session_id=session_id, participants=list(participants))

    async def create_group(self, session_id, group_id, members, senders=None):
        return await self._call("create_group", session_id=session_id, group_id=group_id,
                                members=list(members), senders=None if senders is None else list(senders))

    async def set_participants(self, session_id, participants):
        return await self._call("set_participants", session_id=session_id, participants=list(participants))

    async def inject_message(self, session_id, target, content, request_id, *, source="environment"):
        return await self._call("inject", session_id=session_id, target=target, content=content,
                                request_id=request_id, source=source)

    async def activate(self, session_id, target, request_id, *, content="Activation requested by environment."):
        return await self._call("inject", session_id=session_id, target=target, content=content,
                                request_id=request_id, kind="activation")

    async def get_agent_state(self, agent_id):
        return await self._work("state", agent_id=agent_id)

    async def get_session_state(self, session_id):
        return await self._work("state", session_id=session_id)

    async def metrics(self):
        result = await self._work("state", metrics=True)
        result["runtime_state"] = self._state
        result["task_count"] = len(self._active)
        result["fault"] = self._fault
        return result

    async def read_memory(self, agent_id):
        state = await self.get_agent_state(agent_id)
        return {"content": state["memory"], "revision": state["memory_ref"], "exported_revision": state["exported_ref"]}

    async def read_message(self, message_id):
        return await self._work("get_message", message_id=message_id)

    async def import_memory(self, agent_id, text=None, *, expected_revision=None):
        return await self._call("edit", agent_id=agent_id, kind="memory", text=text, expected_revision=expected_revision)

    async def reload_identity(self, agent_id, text=None, *, expected_revision=None):
        return await self._call("edit", agent_id=agent_id, kind="identity", text=text, expected_revision=expected_revision)

    async def reset_budget(self):
        return await self._call("reset_budget")

    async def start(self):
        if self._closing:
            raise MmarError("runtime_closing")
        if self._state != "CREATED":
            raise MmarError("invalid_runtime_transition")
        await self._work("control", state="RUNNING")
        self._state = "RUNNING"
        self._scheduler = asyncio.create_task(self._schedule(), name="mmar-scheduler")
        self._exporter = asyncio.create_task(self._export_loop(), name="mmar-exporter")
        self._notify()

    def _finished(self, agent, task):
        self._active.pop(agent, None)
        if not task.cancelled():
            error = task.exception()
            if error:
                self._fault = "step_infrastructure_failure"
                self._state = "FAILED"
        self._notify()

    async def _schedule(self):
        try:
            while self._state not in ("STOPPED", "FAILED"):
                event = self._changed
                if not self._closing and self._state == "RUNNING" and len(self._active) < self.limits.concurrency:
                    try:
                        async with self._dispatch_lock:
                            if self._state != "RUNNING":
                                continue
                            snapshot = await self._work("claim", excluded=list(self._active))
                            if snapshot is not None:
                                task = asyncio.create_task(self._step(snapshot), name="mmar-step:" + snapshot.agent_id)
                                self._active[snapshot.agent_id] = (snapshot, task)
                                task.add_done_callback(lambda t, a=snapshot.agent_id: self._finished(a, t))
                    except BudgetExceeded:
                        self._notify()
                        continue
                    if snapshot is not None:
                        continue
                    stats = await self._work("state")
                    if stats["state"] == "BUDGET_EXCEEDED" and not self._active:
                        self._state = "STOPPED"
                        self._notify()
                        break
                    # A rejected oversized head message can leave more pending work.
                    if stats["pending"] and stats["state"] == "RUNNING" and not self._active:
                        await asyncio.sleep(0)
                        continue
                await event.wait()
            if self._state == "FAILED":
                await self._work("stop", mode="cancel")
                for _, task in list(self._active.values()):
                    task.cancel()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fault = "scheduler_infrastructure_failure"
            self._state = "FAILED"
            try:
                await self._work("stop", mode="cancel")
            finally:
                for _, task in list(self._active.values()):
                    task.cancel()
                self._notify()

    async def _step(self, snapshot):
        try:
            async with asyncio.timeout(self.limits.step_timeout):
                repair = False
                for attempt_index in range(self.limits.attempts):
                    attempt = await self._work("reserve", step_id=snapshot.step_id)
                    try:
                        async with asyncio.timeout(self.limits.request_timeout):
                            reply = await self.model.complete(snapshot, repair=repair)
                    except (TimeoutError, TransientModelError) as error:
                        code = error.code if isinstance(error, MmarError) else "request_timeout"
                        await self._work("finish_attempt", attempt_id=attempt, error=code)
                        if attempt_index + 1 == self.limits.attempts:
                            raise MmarError(code) from None
                        await asyncio.sleep(self.limits.retry_delay * 2 ** attempt_index)
                        continue
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        code = error.code if isinstance(error, MmarError) else "model_error"
                        await self._work("finish_attempt", attempt_id=attempt, error=code)
                        raise MmarError(code) from None
                    await self._work("finish_attempt", attempt_id=attempt, usage=reply.usage)
                    try:
                        decision = Decision.parse(reply.output, self.limits)
                    except InvalidDecision:
                        if repair or attempt_index + 1 == self.limits.attempts:
                            raise
                        repair = True
                        continue
                    await self._work("commit", step_id=snapshot.step_id, decision=decision)
                    return
        except asyncio.CancelledError:
            await self._work("fail", step_id=snapshot.step_id, reason="cancelled", state="cancelled")
            raise
        except (MmarError, TimeoutError) as error:
            reason = error.code if isinstance(error, MmarError) else "step_timeout"
            await self._work("fail", step_id=snapshot.step_id, reason=reason)
        finally:
            self._notify()

    async def _wait_tasks(self, predicate, timeout):
        tasks = [task for snapshot, task in list(self._active.values()) if predicate(snapshot)]
        if not tasks:
            return True
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        return not pending

    async def pause(self, timeout=30):
        if self._state == "PAUSED":
            return
        if self._state not in ("RUNNING", "PAUSING"):
            raise MmarError("invalid_runtime_transition")
        async with self._dispatch_lock:
            await self._work("control", state="PAUSING")
            self._state = "PAUSING"
        self._notify()
        if not await self._wait_tasks(lambda _: True, timeout):
            raise MmarError("pause_timeout")
        await self._work("control", state="PAUSED")
        self._state = "PAUSED"
        self._notify()

    async def resume(self):
        if self._closing:
            raise MmarError("runtime_closing")
        if self._state != "PAUSED":
            raise MmarError("invalid_runtime_transition")
        await self._work("control", state="RUNNING")
        self._state = "RUNNING"
        self._notify()

    async def stop(self, mode="cancel", timeout=10):
        if mode not in ("cancel", "drain"):
            raise ValueError("mode must be cancel or drain")
        if self._closed or self._state == "STOPPED":
            return
        async with self._dispatch_lock:
            await self._work("stop", mode=mode)
            self._state = "STOPPING"
        self._notify()
        if self._scheduler:
            self._scheduler.cancel()
            await asyncio.gather(self._scheduler, return_exceptions=True)
        if mode == "drain" and not await self._wait_tasks(lambda _: True, timeout):
            await self._work("stop", mode="cancel")
            mode = "cancel"
        if mode == "cancel":
            for _, task in list(self._active.values()):
                task.cancel()
        if not await self._wait_tasks(lambda _: True, timeout):
            raise MmarError("stop_timeout")
        await self._work("state")  # settle admitted commands before reporting completion
        self._state = "STOPPED"
        self._notify()

    async def stop_session(self, session_id, mode="cancel", timeout=10):
        if mode not in ("cancel", "drain"):
            raise ValueError("mode must be cancel or drain")
        async with self._dispatch_lock:
            await self._call("stop", session_id=session_id, mode=mode)
        predicate = lambda s: s.session_id == session_id
        if mode == "drain" and not await self._wait_tasks(predicate, timeout):
            await self._call("stop", session_id=session_id, mode="cancel")
            mode = "cancel"
        if mode == "cancel":
            for snapshot, task in list(self._active.values()):
                if predicate(snapshot):
                    task.cancel()
        if not await self._wait_tasks(predicate, timeout):
            raise MmarError("stop_timeout")
        self._notify()

    async def unregister_agent(self, agent_id, timeout=10):
        async with self._dispatch_lock:
            await self._call("retire", agent_id=agent_id)
        entry = self._active.get(agent_id)
        if entry:
            entry[1].cancel()
        if not await self._wait_tasks(lambda s: s.agent_id == agent_id, timeout):
            raise MmarError("unregister_timeout")

    async def wait_idle(self, session_id=None, timeout=30):
        async with asyncio.timeout(timeout):
            while True:
                event = self._changed
                stats = await self._work("state", session_id=session_id)
                active = any(session_id is None or s.session_id == session_id for s, _ in self._active.values())
                if not stats["pending"] and not stats["active"] and not active:
                    if self._fault:
                        raise MmarError(self._fault)
                    return stats
                if self._fault:
                    raise MmarError(self._fault)
                await event.wait()

    async def read_events(self, after_seq=0, limit=100):
        return await self._store.call("events", after=after_seq, limit=limit)

    async def events(self, after_seq=0):
        cursor = after_seq
        while not self._closed:
            event = self._changed
            batch = await self.read_events(cursor)
            for item in batch:
                cursor = item["seq"]
                yield item
            if not batch:
                if self._state in ("STOPPED", "FAILED"):
                    return
                await event.wait()

    def _write_export(self, job):
        directory = Path(job["directory"]).resolve()
        destination = directory / "memory.md"
        if not directory.is_relative_to(self._store.root) or not destination.resolve().is_relative_to(self._store.root):
            raise MmarError("path_outside_agent_root")
        directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".memory-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
                output.write(job["body"])
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    async def _export_batch(self):
        async with self._export_lock:
            if self._export_batch_task is None or self._export_batch_task.done():
                jobs = await self._work("exports")
                if not jobs:
                    return 0
                self._export_batch_task = asyncio.create_task(self._write_export_batch(jobs), name="mmar-export-batch")
                self._export_batch_task.add_done_callback(_observe_task)
            task = self._export_batch_task
        # Cancellation cannot interrupt an admitted file write or its DB receipt.
        return await asyncio.shield(task)

    async def _write_export_batch(self, jobs):
        for job in jobs:
            try:
                await asyncio.to_thread(self._write_export, job)
            except Exception:
                await self._work("export_failed", agent=job["agent"], revision=job["revision"])
            else:
                await self._work("exported", agent=job["agent"], revision=job["revision"])
        return len(jobs)

    async def _export_loop(self):
        try:
            while not self._closing:
                event = self._changed
                if await self._export_batch():
                    continue
                try:
                    await asyncio.wait_for(event.wait(), timeout=5)
                except TimeoutError:
                    pass
        except Exception:
            self._fault = "exporter_infrastructure_failure"
            self._notify()

    async def flush_exports(self, timeout=10):
        async with asyncio.timeout(timeout):
            while await self._export_batch():
                pass
        return await self._work("export_status")

    async def close(self, *, timeout=10):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Positive finite cleanup timeout required")
        if self._closed:
            if self._shutdown_task is not None:
                await asyncio.shield(self._shutdown_task)
            return
        if self._shutdown_task is None or self._shutdown_task.done():
            self._closing = True
            self._notify()
            self._shutdown_task = asyncio.create_task(self._shutdown(timeout), name="mmar-runtime-close")
            self._shutdown_task.add_done_callback(_observe_task)
        await asyncio.shield(self._shutdown_task)

    async def _shutdown(self, timeout):
        errors = []
        try:
            await self.stop(timeout=timeout)
        except BaseException as error:
            errors.append(error)
        # A model that ignores cancellation must not lose its Store underneath it.
        if any(not task.done() for _, task in self._active.values()):
            if errors:
                raise errors[0]
            raise MmarError("stop_timeout")
        if self._scheduler and not self._scheduler.done():
            self._scheduler.cancel()
            await asyncio.gather(self._scheduler, return_exceptions=True)
        if self._exporter:
            self._exporter.cancel()
            await asyncio.gather(self._exporter, return_exceptions=True)
        try:
            await self.flush_exports(timeout=timeout)
        except BaseException as error:
            errors.append(error)
        # to_thread file I/O cannot be killed safely. Settle the tracked batch
        # before closing SQLite, even if the optional flush deadline expired.
        if self._export_batch_task is not None:
            try:
                await asyncio.shield(self._export_batch_task)
            except BaseException as error:
                if not any(error is previous for previous in errors):
                    errors.append(error)
        try:
            await _close_adapter(self.model, timeout)
        except BaseException as error:
            errors.append(error)
        finally:
            try:
                await self._store.close()
            except BaseException as error:
                errors.append(error)
            else:
                self._closed = True
                self._state = "STOPPED"
            finally:
                self._notify()
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("Runtime cleanup failed", errors)
