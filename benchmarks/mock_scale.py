"""Offline resource measurement, not a real-LLM throughput claim."""
import argparse
import asyncio
import json
import platform
import tempfile
import time
import tracemalloc
from dataclasses import replace
from pathlib import Path

from mmar import Limits, MockModel, Runtime


def working_set():
    if platform.system() != "Windows":
        return None
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ("peak_working", "working", "peak_paged", "paged",
                "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile", "private")]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return None
    return counters.working


async def measure(count):
    with tempfile.TemporaryDirectory() as directory:
        tracemalloc.start()
        baseline = tracemalloc.get_traced_memory()[0]
        limits = replace(Limits(), steps=count + 100, calls=count + 100,
                         session_steps=count + 100, session_calls=count + 100,
                         directory_bytes=max(16384, count * 24),
                         context_bytes=max(65536, count * 24 + 24576))
        r = await Runtime.open(Path(directory) / "benchmark.db", model=MockModel(delay=0.005), limits=limits)
        try:
            start = time.perf_counter()
            agents = [f"a{i:04}" for i in range(count)]
            for agent in agents:
                await r.register_agent(agent, identity="Short benchmark identity")
            await r.create_session("s", agents)
            registration_seconds = time.perf_counter() - start
            await r.start()
            await asyncio.sleep(0.01)
            idle_bytes = tracemalloc.get_traced_memory()[0] - baseline
            idle_working_set = working_set()
            idle_tasks = len([t for t in asyncio.all_tasks() if t.get_name().startswith("mmar-")])
            await r.pause()
            for agent in agents:
                await r.inject_message("s", agent, "benchmark", agent)
            start = time.perf_counter()
            await r.resume()
            await r.wait_idle(timeout=180)
            elapsed = time.perf_counter() - start
            peak = tracemalloc.get_traced_memory()[1] - baseline
            metrics = await r.metrics()
            return {"agents": count, "concurrency": limits.concurrency, "mock_delay_ms": 5,
                    "registration_seconds": round(registration_seconds, 3),
                    "idle_traced_kib": round(idle_bytes / 1024, 1), "idle_runtime_tasks": idle_tasks,
                    "idle_process_working_set_mib": round(idle_working_set / 1048576, 2) if idle_working_set else None,
                    "run_seconds": round(elapsed, 3), "steps_per_second": round(count / elapsed, 1),
                    "peak_traced_kib": round(peak / 1024, 1), "calls": metrics["calls"],
                    "committed": metrics["step_states"].get("committed", 0)}
        finally:
            await r.close()
            tracemalloc.stop()


async def main(output):
    results = []
    for count in (10, 100, 1000):
        result = await measure(count)
        results.append(result)
        print(json.dumps(result), flush=True)
    report = {"python": platform.python_version(), "platform": platform.platform(),
              "measurement": "tracemalloc Python allocations, excluding interpreter/SQLite native RSS; FULL durability; Mock only", "results": results}
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    asyncio.run(main(parser.parse_args().output))
