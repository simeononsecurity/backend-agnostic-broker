#!/usr/bin/env python3
"""Simulated benchmark of pool routing strategies (ADR 0010).

Drives the real :class:`~broker.manager.ModelManager` (and therefore the real
``cache_aware`` selection logic) against the dependency-free **fake** backend,
while this script *simulates* a per-member KV cache: each member remembers the
prefix blocks it has served (LRU-bounded by a per-scenario capacity), and a
request's simulated time-to-first-token (TTFT) drops in proportion to how much
of its prompt prefix the serving member already holds. This isolates the routing
decision — the only thing that differs between runs is which member each request
lands on.

It ships **named, seeded scenarios** (long-lived sessions, cache pressure,
cross-session prompt sharing, skewed/bursty load) and two headline modes:

* a per-scenario strategy table (``round_robin``/``least_busy``/``sticky``/
  ``cache_aware``), and
* a **β-sweep matrix** (``--matrix``) that, for every scenario, prints
  ``sticky`` as the baseline and ``cache_aware`` at a range of β values, so the
  default β can be chosen from data (hit rate, TTFT p50/p95, load stdev/spread).

The script inserts the repo root on ``sys.path`` itself, so it runs from
anywhere with **no** ``PYTHONPATH=.`` needed:

    python scripts/bench_routing.py                  # default scenario, all strategies
    python scripts/bench_routing.py --matrix         # scenario × β matrix (headline)
    python scripts/bench_routing.py --scenario cache_pressure
    python scripts/bench_routing.py --capacity-sweep # long_lived across cache sizes
    python scripts/bench_routing.py --smoke          # tiny, fast (CI smoke)
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import sys
import tempfile
import textwrap
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path

# Make `import broker` resolve to this worktree without PYTHONPATH=. being set.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from broker.cache_routing import block_hashes, normalize_prompt  # noqa: E402
from broker.config import Settings  # noqa: E402
from broker.manager import ModelManager  # noqa: E402
from broker.registry import load_registry  # noqa: E402

STRATEGIES = ["round_robin", "least_busy", "sticky", "cache_aware"]

#: β values swept in the matrix (0 = pure affinity → larger = spread harder).
DEFAULT_BETAS = [0.0, 0.1, 0.25, 0.5, 1.0, 2.0]

# Simulated TTFT model (milliseconds): a full prefix cache hit saves TTFT_SAVED.
TTFT_BASE_MS = 1000.0
TTFT_SAVED_MS = 800.0

#: Base system-prompt templates; distinct prompts are built by indexing these
#: and are padded long enough to span several prefix blocks.
_PROMPT_TEMPLATES = [
    "You are a meticulous assistant. Follow the house style guide exactly and cite sources. ",
    "You are a terse code reviewer. Point out bugs, security issues, and style violations. ",
    "You are a friendly travel planner. Suggest itineraries with budgets and local tips. ",
    "You are a patient math tutor. Show each step and explain the underlying intuition. ",
    "You are a careful legal summarizer. Extract obligations, parties, and deadlines only. ",
    "You are a data analyst. Describe trends, call out anomalies, and avoid speculation. ",
    "You are a concise support agent. Resolve the ticket and confirm the next action. ",
    "You are a thorough researcher. Compare sources and flag disagreements explicitly. ",
]


@dataclass(frozen=True)
class Scenario:
    """A named, seeded routing workload + simulated cache sizing."""

    name: str
    description: str
    members: int
    sessions: int
    requests: int
    system_prompts: int
    cache_capacity: int  # simulated KV blocks retained per member (LRU)
    concurrency: int  # requests in flight per batch (burstiness)
    skew: float  # Zipf exponent on session popularity (0 = uniform)
    block_chars: int = 128
    seed: int = 1234


#: The four required scenarios. Seeds are fixed so every run is reproducible.
SCENARIOS: dict[str, Scenario] = {
    "long_lived": Scenario(
        name="long_lived",
        description="Few long-lived sessions, 3 shared system prompts, ample cache "
        "(low cache pressure — the original representative run).",
        members=4,
        sessions=150,
        requests=600,
        system_prompts=3,
        cache_capacity=2000,
        concurrency=8,
        skew=0.0,
    ),
    "cache_pressure": Scenario(
        name="cache_pressure",
        description="Same traffic shape but a small per-member cache and more "
        "distinct prefixes, so naive spreading thrashes the KV cache.",
        members=4,
        sessions=150,
        requests=600,
        system_prompts=8,
        cache_capacity=24,
        concurrency=8,
        skew=0.0,
    ),
    "cross_session": Scenario(
        name="cross_session",
        description="Many short sessions sharing a few system prompts — reuse is "
        "cross-session (what sticky-by-session cannot capture).",
        members=4,
        sessions=320,
        requests=600,
        system_prompts=4,
        cache_capacity=28,
        concurrency=8,
        skew=0.0,
    ),
    "skewed_bursty": Scenario(
        name="skewed_bursty",
        description="Skewed session popularity (Zipf) under bursty concurrency — "
        "a few hot prefixes plus heavy in-flight load.",
        members=4,
        sessions=80,
        requests=600,
        system_prompts=4,
        cache_capacity=48,
        concurrency=32,
        skew=1.1,
    ),
}


def _system_prompts(n: int) -> list[str]:
    """Build ``n`` distinct system prompts, each long enough to span blocks."""
    prompts = []
    for i in range(n):
        base = _PROMPT_TEMPLATES[i % len(_PROMPT_TEMPLATES)]
        # Prefix an index so prompts are distinct, then pad to several blocks.
        prompts.append(f"[persona {i:02d}] " + base * 8)
    return prompts


def _registry_yaml(n_members: int) -> str:
    rows = "\n".join(
        f"  - alias: node-{i}\n    backend: fake\n    runtime_model_id: shared-model\n    pool: chat"
        for i in range(n_members)
    )
    return "models:\n" + rows


def _make_manager(tmp: Path, sc: Scenario, strategy: str, beta: float) -> ModelManager:
    reg_path = tmp / f"registry-{strategy}-{beta}.yaml"
    reg_path.write_text(textwrap.dedent(_registry_yaml(sc.members)).strip())
    settings = Settings(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(reg_path),
        enabled_backends=["fake"],
        allowed_model_roots=[str(tmp)],
        health_probe_enabled=False,
        model_idle_timeout_seconds=1000,
        max_loaded_models=sc.members + 2,
        per_model_max_concurrency=max(8, sc.concurrency),
        max_concurrent_requests=max(64, sc.concurrency * 2),
        route_strategy=strategy,
        cache_aware_block_chars=sc.block_chars,
        cache_aware_beta=beta,
    )
    reg = load_registry(settings.registry_path, set(settings.enabled_backends))
    mgr = ModelManager(reg, settings)
    mgr.validate_all()
    return mgr


def _build_workload(sc: Scenario, rng: random.Random) -> list[dict]:
    """Multi-session chat requests with shared system prompts + growing history.

    ``system_prompts`` distinct personas are reused across sessions (cross-session
    prefix sharing), and within a session the conversation history grows turn by
    turn (intra-session prefix growth). ``skew`` biases session selection toward a
    few hot sessions (Zipf); ``sessions``/``requests`` set the reuse density.
    """
    prompts = _system_prompts(sc.system_prompts)
    sessions: dict[int, list[dict]] = {}
    for s in range(sc.sessions):
        sessions[s] = [{"role": "system", "content": prompts[s % len(prompts)]}]
    if sc.skew > 0:
        weights = [1.0 / ((i + 1) ** sc.skew) for i in range(sc.sessions)]
    else:
        weights = None
    requests: list[dict] = []
    population = list(range(sc.sessions))
    for _ in range(sc.requests):
        if weights is not None:
            s = rng.choices(population, weights=weights, k=1)[0]
        else:
            s = rng.randrange(sc.sessions)
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
    mgr: ModelManager, sc: Scenario, strategy: str, beta: float, requests: list[dict]
) -> dict:
    """Replay the workload in concurrent batches so in-flight load actually
    accumulates (that is what lets the load-aware term spread hot prefixes)."""
    from broker.adapters.fake import FakeAdapter

    members = [m.entry.alias for m in mgr.pools["chat"]]
    caches = {a: SimCache(sc.cache_capacity) for a in members}
    counts = dict.fromkeys(members, 0)
    ttfts: list[float] = []
    hit_fracs: list[float] = []

    async def one(req: dict) -> None:
        payload = req["payload"]
        blocks = block_hashes(normalize_prompt(payload), sc.block_chars)
        result = await mgr.chat("chat", payload, "r", route_key=f"sess-{req['session']}")
        served = result["choices"][0]["message"]["content"].split(": ", 1)[1]
        counts[served] += 1
        frac = caches[served].hit_fraction(blocks)
        hit_fracs.append(frac)
        ttfts.append(TTFT_BASE_MS - frac * TTFT_SAVED_MS)
        caches[served].add(blocks)

    FakeAdapter.chat_delay = 0.003  # overlap requests within a batch
    try:
        for start in range(0, len(requests), sc.concurrency):
            batch = requests[start : start + sc.concurrency]
            await asyncio.gather(*(one(r) for r in batch))
    finally:
        FakeAdapter.chat_delay = 0.0
        await mgr.shutdown()
    ttfts.sort()
    load_values = list(counts.values())
    label = strategy if strategy != "cache_aware" else f"cache_aware β={beta:g}"
    return {
        "label": label,
        "strategy": strategy,
        "beta": beta,
        "hit_rate": statistics.mean(hit_fracs) if hit_fracs else 0.0,
        "ttft_p50": _pct(ttfts, 50),
        "ttft_p95": _pct(ttfts, 95),
        "load_stdev": statistics.pstdev(load_values) if len(load_values) > 1 else 0.0,
        "load_spread": f"{min(load_values)}-{max(load_values)}",
        "load_min": min(load_values),
        "load_max": max(load_values),
    }


async def run_one(sc: Scenario, strategy: str, beta: float = 1.0) -> dict:
    """Public entry point (used by the regression test): run one strategy on a
    scenario and return its metrics row. Deterministic for a fixed scenario."""
    rng = random.Random(sc.seed)
    requests = _build_workload(sc, rng)
    with tempfile.TemporaryDirectory() as td:
        mgr = _make_manager(Path(td), sc, strategy, beta)
        return await _run_strategy(mgr, sc, strategy, beta, requests)


def _pct(sorted_vals: list[float], pct: int) -> float:
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, round((pct / 100) * (len(sorted_vals) - 1))))
    return sorted_vals[k]


def _format_table(rows: list[dict], first_col: str = "strategy") -> str:
    header = f"| {first_col} | prefix hit rate | TTFT p50 (ms) | TTFT p95 (ms) | load stdev | load spread |"
    sep = "|---|---|---|---|---|---|"
    lines = [header, sep]
    for r in rows:
        lines.append(
            f"| {r['label']} | {r['hit_rate'] * 100:.1f}% | {r['ttft_p50']:.0f} | "
            f"{r['ttft_p95']:.0f} | {r['load_stdev']:.2f} | {r['load_spread']} |"
        )
    return "\n".join(lines)


async def _scenario_strategies(sc: Scenario, beta: float) -> list[dict]:
    rng = random.Random(sc.seed)
    requests = _build_workload(sc, rng)
    rows = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for strategy in STRATEGIES:
            mgr = _make_manager(tmp, sc, strategy, beta)
            rows.append(await _run_strategy(mgr, sc, strategy, beta, requests))
    return rows


async def _scenario_beta_sweep(sc: Scenario, betas: list[float]) -> list[dict]:
    """sticky baseline + cache_aware across β for one scenario (shared workload)."""
    rng = random.Random(sc.seed)
    requests = _build_workload(sc, rng)
    rows = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mgr = _make_manager(tmp, sc, "sticky", 0.0)
        rows.append(await _run_strategy(mgr, sc, "sticky", 0.0, requests))
        for beta in betas:
            mgr = _make_manager(tmp, sc, "cache_aware", beta)
            rows.append(await _run_strategy(mgr, sc, "cache_aware", beta, requests))
    return rows


async def _main_async(args: argparse.Namespace) -> int:
    betas = DEFAULT_BETAS if not args.smoke else [0.0, 0.25, 1.0]

    if args.matrix:
        scenarios = list(SCENARIOS.values())
        print("\nCache-aware routing — scenario × β matrix (sticky baseline per scenario)\n")
        sticky_by_scn: dict[str, dict] = {}
        ca_default_by_scn: dict[str, dict] = {}
        for sc in scenarios:
            rows = await _scenario_beta_sweep(sc, betas)
            print(f"### {sc.name} — {sc.description}")
            print(
                f"(members={sc.members}, sessions={sc.sessions}, requests={sc.requests}, "
                f"system_prompts={sc.system_prompts}, cache_capacity={sc.cache_capacity}, "
                f"concurrency={sc.concurrency}, skew={sc.skew})\n"
            )
            print(_format_table(rows, first_col="strategy"))
            print()
            sticky_by_scn[sc.name] = next(r for r in rows if r["strategy"] == "sticky")
            ca_default_by_scn[sc.name] = next(
                r for r in rows if r["strategy"] == "cache_aware" and r["beta"] == args.beta
            )
        # Exit-code sanity: at the DEFAULT β, cache_aware must beat sticky on hit
        # rate AND p95 in the cache-pressure and cross-session scenarios.
        ok = True
        for name in ("cache_pressure", "cross_session"):
            ca, st = ca_default_by_scn[name], sticky_by_scn[name]
            better = ca["hit_rate"] + 1e-9 >= st["hit_rate"] and ca["ttft_p95"] <= st["ttft_p95"]
            flag = "OK" if better else "FAIL"
            print(
                f"[{flag}] {name}: cache_aware β={args.beta:g} hit {ca['hit_rate'] * 100:.1f}% / "
                f"p95 {ca['ttft_p95']:.0f} vs sticky hit {st['hit_rate'] * 100:.1f}% / "
                f"p95 {st['ttft_p95']:.0f}"
            )
            ok = ok and better
        return 0 if ok else 1

    if args.capacity_sweep:
        base = SCENARIOS["long_lived"]
        caps = [16, 24, 32, 48, 64, 128, 2000]
        print("\nCache-aware vs sticky under a cache-capacity sweep (long_lived traffic)\n")
        print(f"| cache_capacity | sticky hit / p95 | cache_aware β={args.beta:g} hit / p95 |")
        print("|---|---|---|")
        for cap in caps:
            sc = replace(base, cache_capacity=cap)
            st = await run_one(sc, "sticky")
            ca = await run_one(sc, "cache_aware", args.beta)
            print(
                f"| {cap} | {st['hit_rate'] * 100:.1f}% / {st['ttft_p95']:.0f} | "
                f"{ca['hit_rate'] * 100:.1f}% / {ca['ttft_p95']:.0f} |"
            )
        return 0

    # Default: one scenario, all strategies.
    sc = SCENARIOS[args.scenario]
    if args.smoke:
        sc = replace(sc, members=3, sessions=20, requests=60, concurrency=4)
    rows = await _scenario_strategies(sc, args.beta)
    print(f"\nRouting benchmark — scenario '{sc.name}' at β={args.beta:g}")
    print(f"{sc.description}")
    print(
        f"(members={sc.members}, sessions={sc.sessions}, requests={sc.requests}, "
        f"system_prompts={sc.system_prompts}, cache_capacity={sc.cache_capacity}, "
        f"concurrency={sc.concurrency}, skew={sc.skew})\n"
    )
    print(_format_table(rows))
    print()
    by = {r["strategy"]: r for r in rows}
    if by["cache_aware"]["hit_rate"] + 1e-9 < by["round_robin"]["hit_rate"]:
        print("WARNING: cache_aware hit rate below round_robin (unexpected).")
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--scenario",
        choices=sorted(SCENARIOS),
        default="long_lived",
        help="named scenario for the single-scenario strategy table",
    )
    p.add_argument("--matrix", action="store_true", help="scenario × β matrix (headline)")
    p.add_argument(
        "--capacity-sweep",
        action="store_true",
        help="sticky vs cache_aware across cache capacities (long_lived traffic)",
    )
    p.add_argument(
        "--beta",
        type=float,
        default=Settings().cache_aware_beta,
        help="cache_aware β for single-scenario / sweep / matrix-default (default: config default)",
    )
    p.add_argument("--seed", type=int, default=None, help="override every scenario seed")
    p.add_argument("--smoke", action="store_true", help="tiny fast run for CI")
    args = p.parse_args()
    if args.seed is not None:
        for k, sc in SCENARIOS.items():
            SCENARIOS[k] = replace(sc, seed=args.seed)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
