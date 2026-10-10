"""Opt-in real-provider ring test. Credentials are read only from environment."""
import argparse
import asyncio
import json
import os
import platform
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from benchmarks.mock_scale import working_set
from mmar import Limits, Runtime
from mmar.http_model import ChatCompletionsModel


class ObservedModel:
    def __init__(self, inner):
        self.inner = inner
        self.active = self.peak = 0
        self.latencies = []
        self.http_statuses = {}
        inner._client.event_hooks['response'] = [self.observe_response]

    async def observe_response(self, response):
        status = str(response.status_code)
        self.http_statuses[status] = self.http_statuses.get(status, 0) + 1

    async def complete(self, snapshot, *, repair=False):
        start = time.perf_counter()
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            return await self.inner.complete(snapshot, repair=repair)
        finally:
            self.active -= 1
            self.latencies.append(time.perf_counter() - start)

    async def aclose(self):
        await self.inner.aclose()


async def main(args):
    root = Path(args.data).resolve()
    root.mkdir(parents=True, exist_ok=True)
    run_id = 'live-' + uuid.uuid4().hex[:10]
    db = root / (run_id + '.db')
    limits = Limits(concurrency=args.concurrency, batch_size=1, attempts=2,
        calls=args.calls, session_calls=args.calls, agent_calls=6,
        steps=args.agents * 3, session_steps=args.agents * 3, agent_steps=3,
        request_timeout=45, step_timeout=100)
    model = ObservedModel(ChatCompletionsModel.from_env(
        max_connections=args.concurrency, timeout=45, max_output_tokens=args.output_tokens,
        requests_per_second=args.rate))
    runtime = await Runtime.open(db, model=model, limits=limits)
    started = time.perf_counter()
    report = {'run_id': run_id, 'model': os.environ.get('MMAR_MODEL'),
        'agents': args.agents, 'concurrency_limit': args.concurrency,
        'call_limit': args.calls, 'output_token_limit': args.output_tokens,
        'requests_per_second_limit': args.rate, 'python': platform.python_version(),
        'workload': 'Each agent sends one short idea to its next peer; receivers update memory and become silent.'}
    monitor = None
    peak_working = working_set() or 0
    async def watch():
        nonlocal peak_working
        last = 0
        while True:
            peak_working = max(peak_working, working_set() or 0)
            elapsed = time.perf_counter() - started
            if elapsed - last >= 10:
                metrics = await runtime.metrics()
                print(json.dumps({'elapsed_seconds': round(elapsed, 1),
                    'calls': metrics['calls'], 'steps': metrics['step_states'],
                    'pending': metrics['pending'], 'active': metrics['active']}), flush=True)
                last = elapsed
            await asyncio.sleep(1)
    try:
        agents = [f'agent{i:03}' for i in range(args.agents)]
        for i, agent in enumerate(agents):
            target = agents[(i + 1) % len(agents)]
            await runtime.register_agent(agent, identity=(
                f'你是 {agent}，参加轻量多 Agent 系统讨论，下一位同伴是 {target}。'
                '这是一次有限的真实模型通信测试。严格按 messages 中本次消息的 kind 判断：'
                f'若有 external 消息，只执行一个 send，targets 为 ["{target}"]，'
                'content 为一条 40 字以内的资源节省建议，memory_replace 保存你已发送的简短 Markdown 记录。'
                '若只有 direct 或 multicast 消息，actions 必须为 []，'
                'memory_replace 保存收到的观点和你已完成测试的短记录。不要回复，不要广播。'))
        await runtime.create_session('ring', agents)
        await runtime.start()
        await asyncio.sleep(0)
        report['idle_runtime_tasks'] = len([t for t in asyncio.all_tasks() if t.get_name().startswith('mmar-')])
        report['idle_process_working_set_mib'] = round((working_set() or 0) / 1048576, 2)
        await runtime.pause()
        for agent in agents:
            await runtime.inject_message('ring', agent,
                '请向你的下一位同伴发送一条节省资源的具体建议，然后等待接收。', 'seed-' + agent)
        started = time.perf_counter()
        monitor = asyncio.create_task(watch())
        await runtime.resume()
        try:
            session = await runtime.wait_idle('ring', timeout=args.timeout)
            report['session_state'] = session['state']
            report['timed_out'] = False
        except TimeoutError:
            report['timed_out'] = True
            await runtime.stop(timeout=10)
        report['run_seconds'] = round(time.perf_counter() - started, 3)
        report['metrics'] = await runtime.metrics()
    finally:
        if monitor:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        await runtime.close()
    report['peak_adapter_calls'] = model.peak
    report['http_statuses'] = model.http_statuses
    report['sampled_peak_process_working_set_mib'] = round(peak_working / 1048576, 2)
    if model.latencies:
        ordered = sorted(model.latencies)
        report['adapter_latency_seconds'] = {'mean': round(sum(ordered) / len(ordered), 3),
            'max': round(ordered[-1], 3)}
    with closing(sqlite3.connect(db)) as connection:
        report['session_state_after_close'] = connection.execute(
            "SELECT state FROM sessions WHERE id='ring'").fetchone()[0]
        report['agents_with_committed_steps'] = connection.execute(
            "SELECT count(DISTINCT agent_id) FROM steps WHERE state='committed'").fetchone()[0]
        report['agents_with_updated_memory'] = connection.execute(
            "SELECT count(*) FROM agents WHERE memory_ref != (SELECT min(id) FROM texts WHERE agent_id=agents.id AND kind='memory')").fetchone()[0]
        report['step_errors'] = dict(connection.execute(
            "SELECT error,count(*) FROM steps WHERE error IS NOT NULL GROUP BY error"))
        report['attempt_errors'] = dict(connection.execute(
            "SELECT error,count(*) FROM attempts WHERE error IS NOT NULL GROUP BY error"))
        report['delivery_states'] = dict(connection.execute('SELECT state,count(*) FROM deliveries GROUP BY state'))
        report['schema_repair_calls'] = connection.execute(
            "SELECT coalesce(sum(n-1),0) FROM (SELECT count(*) n FROM attempts WHERE error IS NULL GROUP BY step_id HAVING count(*)>1)").fetchone()[0]
        report['agent_messages'] = [dict(sender=row[0], targets=json.loads(row[1]), content=row[2])
            for row in connection.execute("SELECT sender,target_json,content FROM messages WHERE kind='direct' ORDER BY rowid")]
        report['integrity_check'] = connection.execute('PRAGMA integrity_check').fetchone()[0]
    expected_targets = {agent: [agents[(i + 1) % len(agents)]] for i, agent in enumerate(agents)}
    report['ring_targets_valid'] = (len(report['agent_messages']) == args.agents
        and len({message['sender'] for message in report['agent_messages']}) == args.agents
        and all(message['targets'] == expected_targets.get(message['sender']) for message in report['agent_messages']))
    report['success'] = (not report['timed_out'] and not report['step_errors']
        and report['agents_with_committed_steps'] == args.agents
        and report['agents_with_updated_memory'] == args.agents
        and report['ring_targets_valid']
        and report['delivery_states'] == {'consumed': args.agents * 2}
        and report['metrics']['pending'] == 0 and report['integrity_check'] == 'ok')
    output = root / (run_id + '.json')
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    key = os.environ.get('MMAR_API_KEY', '')
    if key:
        serialized = serialized.replace(key, '[REDACTED]')
    output.write_text(serialized + '\n', encoding='utf-8')
    print(serialized, flush=True)
    print('Report:', output, flush=True)
    print('Store:', db, flush=True)
    if not report['success']:
        raise SystemExit(1)


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agents', type=int, default=30)
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--calls', type=int, default=80)
    parser.add_argument('--output-tokens', type=int, default=512)
    parser.add_argument('--rate', type=float, default=2)
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('--data', default='.mmar')
    args = parser.parse_args()
    if not 2 <= args.agents <= 100 or not 1 <= args.concurrency <= 8:
        parser.error('agents must be 2..100 and concurrency 1..8')
    asyncio.run(main(args))
