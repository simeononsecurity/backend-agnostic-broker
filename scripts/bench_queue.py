#!/usr/bin/env python3
"""Labeled, SIMULATED load benchmark for the ADR 0023 admission queue.

This is a deterministic in-process simulation on the **fake** backend — NOT a
real-hardware benchmark. It drives a burst of concurrent requests at a single
saturated model (per-model concurrency `C`, fixed service time `S`) and reports,
for the queue OFF vs ON, the p50/p95 queue-wait and the rejection/timeout rate at
2–3 offered-load levels. Numbers are illustrative of the *mechanism* (bounding,
waiting, shedding), not of production latency.

Run:  PYTHONPATH=. python scripts/bench_queue.py
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

from broker.config import Settings
from broker.errors import QueueFullError, QueueTimeoutError
from broker.manager import ModelManager
from broker.registry import load_registry

SERVICE_TIME_S = 0.02  # fixed per-request service time on the fake backend
CONCURRENCY = 4  # per-model concurrency (serving slots)


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    i = min(len(xs) - 1, int(round((p / 100.0) * (len(xs) - 1))))
    return xs[i]


def _settings(tmp: Path, *, queue_on: bool) -> Settings:
    reg = tmp / "registry.yaml"
    reg.write_text("models:\n  - alias: m\n    backend: fake\n    runtime_model_id: fake-m\n")
    return Settings(
        auth_enabled=False,
        registry_path=str(reg),
        enabled_backends=["fake"],
        allowed_model_roots=[str(tmp)],
        health_probe_enabled=False,
        per_model_max_concurrency=CONCURRENCY,
        max_concurrent_requests=1000,
        max_loaded_models=5,
        queue_enabled=queue_on,
        queue_max_depth=200,
        queue_max_depth_per_model=200,
        queue_max_wait_seconds=0.5,
        queue_pump_interval_seconds=0.02,
    )


async def _one(mgr: ModelManager, rid: str, key: str, waits, outcomes):
    from broker.adapters.fake import FakeAdapter

    FakeAdapter.chat_delay = SERVICE_TIME_S
    try:
        r = await mgr.chat("m", {"messages": [{"role": "user", "content": "x"}]}, rid, queue_key=key)
        waits.append(float(r.pop("_broker_queue_wait_ms", 0.0)))
        outcomes.append("ok")
    except QueueFullError:
        outcomes.append("rejected")
    except QueueTimeoutError:
        outcomes.append("timeout")
    except Exception:  # noqa: BLE001
        outcomes.append("error")


async def _run(queue_on: bool, n: int, keys: int) -> dict:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        s = _settings(tmp, queue_on=queue_on)
        reg = load_registry(s.registry_path, {"fake"})
        mgr = ModelManager(reg, s)
        mgr.validate_all()
        await mgr.start_queue_pump()
        # warm the model
        await mgr.chat("m", {"messages": [{"role": "user", "content": "x"}]}, "warm")
        waits: list[float] = []
        outcomes: list[str] = []
        t0 = time.monotonic()
        # A burst: every request arrives ~at once (worst case for a saturated model).
        tasks = [
            asyncio.create_task(_one(mgr, f"r{i}", f"k{i % keys}", waits, outcomes))
            for i in range(n)
        ]
        await asyncio.gather(*tasks)
        wall = time.monotonic() - t0
        await mgr.shutdown()
    ok = outcomes.count("ok")
    rej = outcomes.count("rejected")
    to = outcomes.count("timeout")
    return {
        "queue": "on" if queue_on else "off",
        "requests": n,
        "served": ok,
        "rejected": rej,
        "timed_out": to,
        "reject_rate": round((rej + to) / n, 3),
        "p50_wait_ms": round(_pct(waits, 50), 1),
        "p95_wait_ms": round(_pct(waits, 95), 1),
        "wall_s": round(wall, 2),
    }


async def main() -> None:
    print("SIMULATED admission-queue benchmark (fake backend; not real hardware)")
    print(f"  per-model concurrency={CONCURRENCY}  service_time={SERVICE_TIME_S*1000:.0f}ms  keys=4")
    print()
    header = (
        f"{'load':>6} {'queue':>5} {'served':>7} {'rejected':>8} {'timeout':>7} "
        f"{'reject%':>7} {'p50_wait':>9} {'p95_wait':>9} {'wall_s':>7}"
    )
    print(header)
    print("-" * len(header))
    for label, n in (("1x", CONCURRENCY), ("2x", CONCURRENCY * 20), ("3x", CONCURRENCY * 60)):
        for queue_on in (False, True):
            r = await _run(queue_on, n, keys=4)
            print(
                f"{label:>6} {r['queue']:>5} {r['served']:>7} {r['rejected']:>8} "
                f"{r['timed_out']:>7} {r['reject_rate']*100:>6.1f}% "
                f"{r['p50_wait_ms']:>8.1f}ms {r['p95_wait_ms']:>8.1f}ms {r['wall_s']:>6.2f}s"
            )
    print()
    print("Read: queue OFF never bounds or times out the wait (default-'queue' policy")
    print("blocks on the semaphore); queue ON bounds the wait and sheds excess with a")
    print("429/503 + Retry-After once depth/wait limits are hit, keeping tail wait bounded.")


if __name__ == "__main__":
    asyncio.run(main())
