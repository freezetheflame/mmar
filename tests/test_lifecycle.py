import asyncio
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mmar import Decision, Limits, MmarError, MockModel, ModelReply, Runtime
from mmar.store import Store


class TrackedModel(MockModel):
    def __init__(self):
        super().__init__()
        self.close_calls = 0

    async def aclose(self):
        self.close_calls += 1


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'runtime.db'
        self.runtimes = []

    async def asyncTearDown(self):
        for runtime in reversed(self.runtimes):
            if not runtime._closed:
                await runtime.close()
        self.temp.cleanup()

    async def open(self, model=None, **kwargs):
        runtime = await Runtime.open(self.path, model=model or TrackedModel(), **kwargs)
        self.runtimes.append(runtime)
        return runtime

    async def assert_released(self, runtime):
        self.assertTrue(runtime._closed)
        self.assertFalse(runtime._store._thread.is_alive())
        self.assertTrue(runtime._store._lock.closed)
        reopened = await self.open()
        await reopened.close()

    async def test_constructor_rejection_closes_adapter(self):
        owner = await self.open()
        model = TrackedModel()
        with self.assertRaisesRegex(MmarError, 'store_in_use'):
            await Runtime.open(self.path, model=model)
        self.assertEqual(model.close_calls, 1)
        self.assertFalse(owner._closed)

    async def test_path_setup_failure_closes_adapter(self):
        root = Path(self.temp.name) / 'not-a-directory'
        root.write_text('file', encoding='utf-8')
        model = TrackedModel()
        with self.assertRaises(OSError):
            await Runtime.open(root / 'runtime.db', model=model)
        self.assertEqual(model.close_calls, 1)

    async def test_worker_start_failure_releases_lock_and_adapter(self):
        model = TrackedModel()
        with patch('mmar.store.threading.Thread.start', side_effect=RuntimeError('thread unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'thread unavailable'):
                await Runtime.open(self.path, model=model)
        self.assertEqual(model.close_calls, 1)
        runtime = await self.open()
        await self.assert_released_after_close(runtime)

    async def assert_released_after_close(self, runtime):
        await runtime.close()
        await self.assert_released(runtime)

    async def test_ready_failure_releases_lock_and_adapter(self):
        owner = await self.open()
        await owner.close()
        model = TrackedModel()
        with self.assertRaisesRegex(MmarError, 'limits_mismatch'):
            await Runtime.open(self.path, model=model, limits=replace(Limits(), concurrency=2))
        self.assertEqual(model.close_calls, 1)
        runtime = await self.open()
        await self.assert_released_after_close(runtime)

    async def test_startup_cleanup_error_preserves_original_failure(self):
        class BadClose(TrackedModel):
            async def aclose(self):
                self.close_calls += 1
                raise RuntimeError('private adapter detail')
        await self.open()
        model = BadClose()
        with self.assertRaisesRegex(MmarError, 'store_in_use') as raised:
            await Runtime.open(self.path, model=model)
        self.assertEqual(model.close_calls, 1)
        self.assertEqual(raised.exception.__notes__, ['Initialization cleanup failed: RuntimeError'])

    async def test_cancelled_open_settles_worker_and_closes_adapter(self):
        entered, release = threading.Event(), threading.Event()
        recover = Store._recover
        def blocked(store):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test release not signaled')
            recover(store)
        model = TrackedModel()
        with patch.object(Store, '_recover', blocked):
            opening = asyncio.create_task(Runtime.open(self.path, model=model))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                opening.cancel()
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(opening, 3)
        self.assertEqual(model.close_calls, 1)
        runtime = await self.open()
        await self.assert_released_after_close(runtime)

    async def test_runtime_construction_failure_cleans_initialized_store(self):
        class BadRuntime(Runtime):
            def __init__(self, *args):
                raise RuntimeError('runtime initialization failed')
        model = TrackedModel()
        with self.assertRaisesRegex(RuntimeError, 'runtime initialization failed'):
            await BadRuntime.open(self.path, model=model)
        self.assertEqual(model.close_calls, 1)
        runtime = await self.open()
        await self.assert_released_after_close(runtime)

    async def test_export_timeout_still_releases_adapter_and_store(self):
        model = TrackedModel()
        runtime = await self.open(model)
        async def fail_export(**kwargs):
            raise TimeoutError('export deadline')
        runtime.flush_exports = fail_export
        with self.assertRaisesRegex(TimeoutError, 'export deadline'):
            await runtime.close()
        await self.assert_released(runtime)
        with self.assertRaisesRegex(TimeoutError, 'export deadline'):
            await runtime.close()
        self.assertEqual(model.close_calls, 1)

    async def test_multiple_cleanup_errors_are_reported_after_store_release(self):
        class BadClose(TrackedModel):
            async def aclose(self):
                self.close_calls += 1
                raise RuntimeError('adapter close failed')
        model = BadClose()
        runtime = await self.open(model)
        async def fail_export(**kwargs):
            raise TimeoutError('export deadline')
        runtime.flush_exports = fail_export
        with self.assertRaises(ExceptionGroup) as raised:
            await runtime.close()
        self.assertEqual([type(error) for error in raised.exception.exceptions], [TimeoutError, RuntimeError])
        self.assertEqual(model.close_calls, 1)
        await self.assert_released(runtime)

    async def test_concurrent_close_survives_caller_cancellation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class SlowClose(TrackedModel):
            async def aclose(self):
                self.close_calls += 1
                entered.set()
                await release.wait()
        model = SlowClose()
        runtime = await self.open(model)
        first = asyncio.create_task(runtime.close())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(runtime.close())
            with self.assertRaisesRegex(MmarError, 'runtime_closing'):
                await runtime.register_agent('late', identity='late')
            with self.assertRaisesRegex(MmarError, 'runtime_closing'):
                await runtime.start()
        finally:
            release.set()
        await asyncio.wait_for(second, 2)
        await runtime.close()
        self.assertEqual(model.close_calls, 1)
        await self.assert_released(runtime)

    async def test_adapter_close_timeout_releases_store(self):
        cancelled = asyncio.Event()
        class HangingClose(TrackedModel):
            async def aclose(self):
                self.close_calls += 1
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
        model = HangingClose()
        runtime = await self.open(model)
        with self.assertRaisesRegex(MmarError, 'model_close_timeout'):
            await runtime.close(timeout=0.2)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(model.close_calls, 1)
        await self.assert_released(runtime)

    async def test_cancelled_flush_keeps_file_write_tracked_until_close(self):
        root = Path(self.temp.name) / 'agents'
        directory = root / 'a'
        directory.mkdir(parents=True)
        (directory / 'agent.md').write_text('identity', encoding='utf-8')
        runtime = await self.open(agent_root=root)
        await runtime.register_agent('a', directory=directory)
        entered, release = threading.Event(), threading.Event()
        original = runtime._write_export
        writes = []
        def blocked(job):
            writes.append(job['revision'])
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test release not signaled')
            original(job)
        runtime._write_export = blocked
        flushing = asyncio.create_task(runtime.flush_exports())
        closing = None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            flushing.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await flushing
            closing = asyncio.create_task(runtime.close())
            await asyncio.sleep(0.02)
            self.assertFalse(closing.done())
            self.assertFalse(runtime._store._closed)
        finally:
            release.set()
        await asyncio.wait_for(closing, 2)
        self.assertEqual(len(writes), 1)
        self.assertEqual((directory / 'memory.md').read_text(encoding='utf-8'), '# Memory')
        await self.assert_released(runtime)

    async def test_stop_timeout_retains_store_until_active_model_settles(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class SlowModel(TrackedModel):
            async def complete(self, snapshot, *, repair=False):
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
                return ModelReply(Decision(memory_replace='late response'))
        model = SlowModel()
        runtime = await self.open(model)
        await runtime.register_agent('a', identity='a')
        await runtime.create_session('s', ['a'])
        await runtime.inject_message('s', 'a', 'input', 'seed')
        await runtime.start()
        try:
            await asyncio.wait_for(entered.wait(), 1)
            with self.assertRaisesRegex(MmarError, 'stop_timeout'):
                await runtime.close(timeout=0.03)
            self.assertFalse(runtime._closed)
            self.assertTrue(runtime._store._thread.is_alive())
        finally:
            release.set()
        await runtime.close(timeout=1)
        self.assertEqual(model.close_calls, 1)
        await self.assert_released(runtime)

    async def test_close_rejects_invalid_deadlines_without_starting_cleanup(self):
        model = TrackedModel()
        runtime = await self.open(model)
        for timeout in (0, -1, True, float('inf'), float('nan')):
            with self.assertRaises(ValueError):
                await runtime.close(timeout=timeout)
        self.assertEqual(model.close_calls, 0)
        self.assertFalse(runtime._closing)
        await self.assert_released_after_close(runtime)
