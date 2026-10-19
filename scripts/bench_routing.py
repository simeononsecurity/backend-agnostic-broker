#!/usr/bin/env python3
"""Simulated benchmark of pool routing strategies (ADR 0010).

Drives the real :class:`~broker.manager.ModelManager` (and therefore the real
``cache_aware`` selection logic) against the dependency-free **fake** backend,
while this script *simulates* a per-member KV cache: each member remembers the
prefix blocks it has served (LRU-bounded), and a request's simulated
time-to-first-token (TTFT) drops in proportion to how much of its prompt prefix
the serving member already holds. This isolates the routing decision — the only
thing that differs between runs is which member each request lands on.

It compares ``round_robin`` / ``least_busy`` / ``sticky`` / ``cache_aware`` on a
shared-prefix, multi-session workload and prints a Markdown table of prefix hit
rate, simulated TTFT p50/p95, and load balance (per-member request spread).

Usage:
    python scripts/bench_routing.py                 # full run, prints a table
    python scripts/bench_routing.py --smoke         # tiny, fast (CI smoke)
    python scripts/bench_routing.py --members 4 --sessions 40 --requests 400
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import tempfile
import textwrap
from collections import OrderedDict
from pathlib import Path

from broker.cache_routing import block_hashes, normalize_prompt
from broker.config import Settings
from broker.manager import ModelManager
from broker.registry import load_registry

STRATEGIES = ["round_robin", "least_busy", "sticky", "cache_aware"]

# Simulated TTFT model (milliseconds): a full prefix cache hit saves TTFT_SAVED.
TTFT_BASE_MS = 1000.0
TTFT_SAVED_MS = 800.0


def _registry_yaml(n_members: int) -> str:
    rows = "\n".join(
        f"  - alias: node-{i}\n    backend: fake\n    runtime_model_id: shared-model\n    pool: chat"
        for i in range(n_members)
    )
    return "models:\n" + rows


def _make_manager(tmp: Path, n_members: int, strategy: str, block_chars: int, beta: float):
    reg_path = tmp / f"registry-{strategy}.yaml"
    reg_path.write_text(textwrap.dedent(_registry_yaml(n_members)).strip())
    settings = Settings(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(reg_path),
        enabled_backends=["fake"],
        allowed_model_roots=[str(tmp)],
        health_probe_enabled=False,
        model_idle_timeout_seconds=1000,
        max_loaded_models=n_members + 2,
        per_model_max_concurrency=8,
        max_concurrent_requests=64,
        route_strategy=strategy,
        cache_aware_block_chars=block_chars,
        cache_aware_beta=beta,
    )
    reg = load_registry(settings.registry_path, set(settings.enabled_backends))
    mgr = ModelManager(reg, settings)
    mgr.validate_all()
    return mgr


def _build_workload(n_sessions: int, n_requests: int, rng: random.Random) -> list[dict]:
    """Multi-session chat requests with shared system prompts + growing history.

    A handful of shared system prompts are reused across sessions (cross-session
    prefix sharing — the system-prompt / few-shot case), and within a session the
    conversation history grows turn by turn (intra-session prefix growth — the
    multi-turn case). Both are what prefix-aware routing exploits.
    """
    system_prompts = [
        "You are a meticulous assistant. Follow the house style guide exactly and "
        "cite sources. " * 4,
        "You are a terse code reviewer. Point out bugs, security issues, and style "
        "violations only. " * 4,
        "You are a friendly travel planner. Suggest itineraries with budgets and local tips. " * 4,
    ]
    sessions: dict[int, list[dict]] = {}
    for s in range(n_sessions):
        sys = system_prompts[s % len(system_prompts)]
        sessions[s] = [{"role": "system", "content": sys}]
    requests: list[dict] = []
    for _ in range(n_requests):
        s = rng.randrange(n_sessions)
        history = sessions[s]
        turn = len(history) // 2
        history = history + [
            {"role": "user", "content": f"Turn {turn}: {'detail ' * rng.randint(3, 12)}"}
        ]
        # Append an assistant placeholder so the NEXT turn's prefix includes it.
        sessions[s] = history + [{"role": "assistant", "content": f"ack {turn} {'resp ' * 5}"}]
        requests.append({"session": s, "payload": {"messages": list(history)}})
    rng.shuffle(requests)
    return requests


class SimCache:
    """Per-member simulated KV cache: an LRU set of block hashes it has served."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.blocks: OrderedDict[str, None] = OrderedDict()

    def hit_fraction(self, blocks: list[str]) -> float:
        if not blocks:
            return 0.0
        matched = 0
        for h in blocks:
            if h in self.blocks:
                matched += 1
            else:
                break  # chained hashes → longest *consecutive* prefix
        return matched / len(blocks)

    def add(self, blocks: list[str]) -> None:
        for h in blocks:
            self.blocks[h] = None
            self.blocks.move_to_end(h)
        while self.capacity > 0 and len(self.blocks) > self.capacity:
            self.blocks.popitem(last=False)


async def _run_strategy(
    mgr: ModelManager,
    strategy: str,
    requests: list[dict],
    block_chars: int,
    cache_capacity: int,
    concurrency: int,
) -> dict:
    """Replay the workload in concurrent batches so in-flight load actually
    accumulates (that is what lets the load-aware term spread hot prefixes)."""
    from broker.adapters.fake import FakeAdapter

    members = [m.entry.alias for m in mgr.pools["chat"]]
    caches = {a: SimCache(cache_capacity) for a in members}
    counts = dict.fromkeys(members, 0)
    ttfts: list[float] = []
    hit_fracs: list[float] = []

    async def one(req: dict) -> None:
        payload = req["payload"]
        blocks = block_hashes(normalize_prompt(payload), block_chars)
        result = await mgr.chat("chat", payload, "r", route_key=f"sess-{req['session']}")
        served = result["choices"][0]["message"]["content"].split(": ", 1)[1]
        counts[served] += 1
        frac = caches[served].hit_fraction(blocks)
        hit_fracs.append(frac)
        ttfts.append(TTFT_BASE_MS - frac * TTFT_SAVED_MS)
        caches[served].add(blocks)

    FakeAdapter.chat_delay = 0.003  # overlap requests within a batch
    try:
        for start in range(0, len(requests), concurrency):
            batch = requests[start : start + concurrency]
            await asyncio.gather(*(one(r) for r in batch))
    finally:
        FakeAdapter.chat_delay = 0.0
        await mgr.shutdown()
    ttfts.sort()
    load_values = list(counts.values())
    return {
        "strategy": strategy,
        "hit_rate": statistics.mean(hit_fracs) if hit_fracs else 0.0,
        "ttft_p50": _pct(ttfts, 50),
        "ttft_p95": _pct(ttfts, 95),
        "load_stdev": statistics.pstdev(load_values) if len(load_values) > 1 else 0.0,
        "load_spread": f"{min(load_values)}-{max(load_values)}",
    }


def _pct(sorted_vals: list[float], pct: int) -> float:
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, round((pct / 100) * (len(sorted_vals) - 1))))
    return sorted_vals[k]


def _format_table(rows: list[dict]) -> str:
    header = (
        "| strategy | prefix hit rate | TTFT p50 (ms) | TTFT p95 (ms) | load stdev | load spread |"
    )
    sep = "|---|---|---|---|---|---|"
    lines = [header, sep]
    for r in rows:
        lines.append(
            f"| {r['strategy']} | {r['hit_rate'] * 100:.1f}% | {r['ttft_p50']:.0f} | "
            f"{r['ttft_p95']:.0f} | {r['load_stdev']:.2f} | {r['load_spread']} |"
        )
    return "\n".join(lines)


async def _main_async(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed)
    requests = _build_workload(args.sessions, args.requests, rng)
    rows = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for strategy in STRATEGIES:
            mgr = _make_manager(tmp, args.members, strategy, args.block_chars, args.beta)
            rows.append(
                await _run_strategy(
                    mgr, strategy, requests, args.block_chars, args.cache_capacity, args.concurrency
                )
            )
    print(
        f"\nRouting benchmark — {args.members} members, {args.sessions} sessions, "
        f"{len(requests)} requests, block_chars={args.block_chars}, beta={args.beta}\n"
    )
    print(_format_table(rows))
    print()
    # Sanity for the smoke test: cache_aware must not be worse than round_robin.
    by = {r["strategy"]: r for r in rows}
    if by["cache_aware"]["hit_rate"] + 1e-9 < by["round_robin"]["hit_rate"]:
        print("WARNING: cache_aware hit rate below round_robin (unexpected).")
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--members", type=int, default=4)
    p.add_argument("--sessions", type=int, default=150)
    p.add_argument("--requests", type=int, default=600)
    p.add_argument("--block-chars", type=int, default=128)
    p.add_argument("--beta", type=float, default=1.0)
    p.add_argument("--cache-capacity", type=int, default=2000, help="blocks per member (LRU)")
    p.add_argument("--concurrency", type=int, default=8, help="requests in flight per batch")
    p.add_argument("--seed", type=int, default=1234)
    p.add_argument("--smoke", action="store_true", help="tiny fast run for CI")
    args = p.parse_args()
    if args.smoke:
        args.members, args.sessions, args.requests, args.concurrency = 3, 8, 40, 4
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
