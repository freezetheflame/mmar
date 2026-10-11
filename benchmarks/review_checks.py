"""Offline reproductions for the v0.1 optimization review; no provider requests.

These are diagnostic observations, not regressions accepted as correct behavior.
Run: python -m benchmarks.review_checks --output .mmar/review-checks.json
"""
import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from mmar import Limits, MmarError, MockModel, Runtime


async def rate_wait_check(root):
    import httpx
    from mmar.http_model import ChatCompletionsModel
    actual_requests = 0

    async def respond(request):
        nonlocal actual_requests
        actual_requests += 1
        return httpx.Response(200, json={
            'choices': [{'message': {'content': '{"actions":[],"memory_replace":null}'}}]})

    limits = Limits(concurrency=4, attempts=1, request_timeout=0.2, step_timeout=2)
    model = ChatCompletionsModel(base_url='https://example.test/v1', model='offline',
        api_key='offline-placeholder', requests_per_second=1, timeout=0.2,
        transport=httpx.MockTransport(respond))
    runtime = await Runtime.open(root / 'rate.db', model=model, limits=limits)
    try:
        agents = ['a', 'b', 'c', 'd']
        for agent in agents:
            await runtime.register_agent(agent, identity=agent)
        await runtime.create_session('s', agents)
        for agent in agents:
            await runtime.inject_message('s', agent, 'offline input', agent)
        await runtime.start()
        state = await runtime.wait_idle('s', timeout=10)
        events = await runtime.read_events(limit=1000)
        errors = [event['data']['reason'] for event in events if event['kind'] == 'step.failed']
        return {'name': 'rate_wait_uses_request_timeout', 'calls_reserved': state['calls'],
            'mock_transport_requests': actual_requests, 'steps': state['step_states'],
            'errors': errors, 'reproduced': actual_requests < state['calls'] and 'request_timeout' in errors}
    finally:
        await runtime.close()


async def export_close_check(root):
    runtime = await Runtime.open(root / 'close.db', model=MockModel())
    original = runtime.flush_exports

    async def fail_export(*args, **kwargs):
        raise TimeoutError('Injected export timeout; no disk failure required.')

    runtime.flush_exports = fail_export
    try:
        error_type = None
        try:
            await runtime.close()
        except Exception as error:
            error_type = type(error).__name__
        result = {'name': 'close_export_failure_leaves_store_open', 'exception': error_type,
            'runtime_closed': runtime._closed, 'store_thread_alive': runtime._store._thread.is_alive()}
        attempted = None
        try:
            attempted = await Runtime.open(root / 'close.db', model=MockModel())
        except MmarError as error:
            result['reopen_error'] = error.code
        finally:
            if attempted:
                await attempted.close()
        result['reproduced'] = (result['store_thread_alive'] and result.get('reopen_error') == 'store_in_use')
        return result
    finally:
        runtime.flush_exports = original
        if not runtime._closed:
            await runtime.close()


async def rejected_open_check(root):
    class TrackedModel(MockModel):
        closed = False

        async def aclose(self):
            self.closed = True

    owner = await Runtime.open(root / 'open.db', model=MockModel())
    rejected_model = TrackedModel()
    attempted = None
    try:
        error_code = None
        try:
            attempted = await Runtime.open(root / 'open.db', model=rejected_model)
        except MmarError as error:
            error_code = error.code
        result = {'name': 'early_open_failure_does_not_close_adapter', 'error': error_code,
            'adapter_closed': rejected_model.closed,
            'reproduced': error_code == 'store_in_use' and not rejected_model.closed}
        return result
    finally:
        if attempted:
            await attempted.close()
        await rejected_model.aclose()
        await owner.close()


async def main(output):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        findings = []
        for check in (rate_wait_check, export_close_check, rejected_open_check):
            findings.append(await check(root))
    report = {'provider_requests': 0, 'mode': 'HTTPX MockTransport and injected export failure',
              'findings': findings}
    rendered = json.dumps(report, indent=2) + '\n'
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding='utf-8')
    print(rendered, end='')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output')
    asyncio.run(main(parser.parse_args().output))
