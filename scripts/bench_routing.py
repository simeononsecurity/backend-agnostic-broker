#!/usr/bin/env python3
"""Simulated benchmark of pool routing strategies with a **queueing model** (ADR 0018).

Drives the real :class:`~broker.manager.ModelManager` selection logic (and therefore
the real ``cache_aware`` scoring) against a **discrete-event simulation** of a pool of
continuous-batching inference servers. Unlike the ADR 0010 harness — whose TTFT was a
pure routing-savings model with *no queueing* (an upper bound) — this version prices
load imbalance as real latency:

* **Service model** (per the LLM-serving simulator literature; see
  ``docs/research/adr-0018-cache-aware-v2.md``): each pool member is an M/G/c-style
  continuous-batching server with ``nmax`` concurrent slots. One engine *step*
  advances every batched sequence by one iteration and costs ``t_iter(n) = W + H·n``
  — so a member running more concurrent sequences runs every one of them slower
  (inference-fleet-sim, arXiv 2603.16054). A request needs
  ``prefill_iters = ceil(uncached_prefix_tokens / chunk)`` then ``decode_iters =
  output_tokens``. **TTFT = queue-wait + prefill-time**; a prefix cache hit shrinks
  ``uncached_prefix_tokens`` and therefore prefill time.
* **Routing is real.** At each arrival the member in-flight load is injected into the
  manager and the real ``_select_member`` picks the target; cache_aware reads the real
  prefix index. Only the *service* is simulated.

It ships named, seeded scenarios and several modes:

* a per-scenario strategy table (``round_robin``/``least_busy``/``sticky``/
  ``cache_aware``), reporting prefix hit rate, **TTFT p50/p95/p99 incl. queueing**,
  **prefill work/req (uncached prefill tokens computed)**, **throughput**, **max
  member utilization**, and load spread;
* a **utilization sweep** (``--sweep``) that runs all four strategies at ~50/70/85/95%
  max utilization (arrival rate calibrated per scenario via least_busy) and then
  reports each strategy's **capacity** — the max sustained arrival rate that keeps
  TTFT p95 under an SLO, found by bisection;
* a **β matrix** (``--matrix``) at a moderate (~85% util) load, printing
  ``least_busy`` and ``sticky`` baselines plus ``cache_aware`` across β per scenario, and
* ``--select-beta``, which applies the β selection rule (p95 within a tolerance of
  ``least_busy`` at ≤85% util + a balance bound) and prints the β it picks (and why).

The script inserts the repo root on ``sys.path`` itself, so it runs from anywhere
with **no** ``PYTHONPATH=.`` needed:

    python scripts/bench_routing.py                  # default scenario, all strategies
    python scripts/bench_routing.py --sweep           # utilization sweep + SLO capacity
    python scripts/bench_routing.py --matrix          # scenario × β matrix (moderate load)
    python scripts/bench_routing.py --select-beta      # apply the β selection rule
    python scripts/bench_routing.py --scenario cache_pressure
    python scripts/bench_routing.py --load-signal chars   # compare load signals (ADR 0018 item 3)
    python scripts/bench_routing.py --smoke          # tiny, fast (CI smoke)
"""

from __future__ import annotations

import argparse
import asyncio
import heapq
import itertools
import math
import random
import statistics
import sys
import tempfile
import textwrap
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

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

#: Load signals the scorer can use (ADR 0018 item 3). "count" = in-flight request
#: count (ADR 0010 default); "chars" = in-flight prompt characters (a dependency-free
#: prefill-work proxy the broker can actually compute); "work" = remaining service
#: iterations (an oracle upper bound needing an output-length predictor the broker
#: does not own — used only to bound the achievable benefit).
LOAD_SIGNALS = ("count", "chars", "work")

#: Base system-prompt templates; distinct prompts are built by indexing these and are
#: padded long enough to span several prefix blocks.
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
    """A named, seeded routing workload + simulated cache + queueing service model."""

    name: str
    description: str
    members: int
    sessions: int
    requests: int
    system_prompts: int
    cache_capacity: int  # simulated KV blocks retained per member (LRU)
    concurrency: int  # legacy field (kept for the workload shape / back-compat)
    skew: float  # Zipf exponent on session popularity (0 = uniform)
    # --- queueing service model (ADR 0018) ---
    arrival_rate: float  # Poisson arrivals, requests/second (offered load)
    member_slots: int = 6  # nmax: continuous-batching concurrency per member
    iter_base_ms: float = 6.0  # W: baseline per-step iteration latency
    iter_per_seq_ms: float = 1.5  # H: added latency per concurrent sequence
    prefill_chunk_tokens: int = 256  # Cchunk: chunked-prefill tokens per step
    chars_per_token: int = 4  # prompt chars → tokens (the broker owns no tokenizer)
    output_tokens_min: int = 32
    output_tokens_max: int = 160
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
        arrival_rate=12.0,
    ),
    "cache_pressure": Scenario(
        name="cache_pressure",
        description="Same traffic shape but a small per-member cache and more distinct "
        "prefixes, so naive spreading thrashes the KV cache.",
        members=4,
        sessions=150,
        requests=600,
        system_prompts=8,
        cache_capacity=24,
        concurrency=8,
        skew=0.0,
        arrival_rate=13.0,
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
        arrival_rate=13.0,
    ),
    "skewed_bursty": Scenario(
        name="skewed_bursty",
        description="Skewed session popularity (Zipf) under bursty, high arrival rate — "
        "a few hot prefixes plus heavy in-flight load.",
        members=4,
        sessions=80,
        requests=600,
        system_prompts=4,
        cache_capacity=48,
        concurrency=32,
        skew=1.1,
        arrival_rate=15.0,
    ),
}


def _system_prompts(n: int) -> list[str]:
    """Build ``n`` distinct system prompts, each long enough to span blocks."""
    prompts = []
    for i in range(n):
        base = _PROMPT_TEMPLATES[i % len(_PROMPT_TEMPLATES)]
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
        per_model_max_concurrency=10_000,  # the DES models concurrency, not the manager
        max_concurrent_requests=10_000,
        route_strategy=strategy,
        cache_aware_block_chars=sc.block_chars,
        cache_aware_beta=beta,
    )
    reg = load_registry(settings.registry_path, set(settings.enabled_backends))
    mgr = ModelManager(reg, settings)
    mgr.validate_all()
    return mgr


@dataclass
class _Req:
    """One simulated request: routing inputs + its sampled service demand."""

    session: int
    payload: dict
    blocks: list[str]
    prompt_chars: int
    prompt_tokens: int
    output_tokens: int
    arrival: float


def _build_workload(sc: Scenario, rng: random.Random) -> list[_Req]:
    """Multi-session chat requests with shared system prompts + growing history, plus
    Poisson arrival times and sampled output lengths (so every strategy replays an
    identical arrival/demand stream — only routing differs)."""
    prompts = _system_prompts(sc.system_prompts)
    sessions: dict[int, list[dict]] = {}
    for s in range(sc.sessions):
        sessions[s] = [{"role": "system", "content": prompts[s % len(prompts)]}]
    if sc.skew > 0:
        weights = [1.0 / ((i + 1) ** sc.skew) for i in range(sc.sessions)]
    else:
        weights = None
    built: list[_Req] = []
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
        sessions[s] = history + [{"role": "assistant", "content": f"ack {turn} {'resp ' * 5}"}]
        payload = {"messages": list(history)}
        text = normalize_prompt(payload)
        built.append(
            _Req(
                session=s,
                payload=payload,
                blocks=block_hashes(text, sc.block_chars),
                prompt_chars=len(text),
                prompt_tokens=max(1, len(text) // sc.chars_per_token),
                output_tokens=rng.randint(sc.output_tokens_min, sc.output_tokens_max),
                arrival=0.0,
            )
        )
    rng.shuffle(built)
    # Poisson arrivals: exponential inter-arrival with mean 1/λ, assigned in replay
    # order. Stored in MILLISECONDS so the whole DES runs in one time unit (ms).
    t = 0.0
    for req in built:
        t += rng.expovariate(sc.arrival_rate)
        req.arrival = t * 1000.0
    return built


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


@dataclass
class _Seq:
    """A request in the pipeline of one member (queued, then in service)."""

    req: _Req
    prefill_iters: int = 0  # set at admission (depends on cache hit then)
    decode_iters: int = 0
    prefill_done: bool = False
    ttft_ms: float = -1.0

    @property
    def remaining_iters(self) -> int:
        return (0 if self.prefill_done else self.prefill_iters) + self.decode_iters


# Event kinds for the discrete-event sim.
_ARRIVAL, _STEP = 0, 1


async def _run_strategy(
    mgr: ModelManager,
    sc: Scenario,
    strategy: str,
    beta: float,
    requests: list[_Req],
    load_signal: str = "count",
) -> dict:
    """Discrete-event simulation of the pool serving ``requests`` under ``strategy``.

    Routing decisions come from the real ``mgr._select_member``; service time, queueing,
    and TTFT are simulated. Returns a metrics row (hit rate, TTFT p50/p95/p99 incl.
    queueing, throughput, utilization, load spread). Deterministic for a fixed seed.
    """
    members = [m.entry.alias for m in mgr.pools["chat"]]
    managed = {m.entry.alias: m for m in mgr.pools["chat"]}
    caches = {a: SimCache(sc.cache_capacity) for a in members}
    active: dict[str, list[_Seq]] = {a: [] for a in members}
    waitq: dict[str, deque[_Seq]] = {a: deque() for a in members}
    step_scheduled: dict[str, bool] = dict.fromkeys(members, False)
    served_counts = dict.fromkeys(members, 0)
    busy_ms = dict.fromkeys(members, 0.0)
    ttfts: list[float] = []
    hit_fracs: list[float] = []
    completions = 0
    prefill_tokens_total = 0  # uncached prefill tokens actually computed (GPU work)

    heap: list[tuple[float, int, int, object]] = []
    order = itertools.count()
    for req in requests:
        heapq.heappush(heap, (req.arrival, next(order), _ARRIVAL, req))

    def _member_signal(alias: str) -> float:
        seqs = list(active[alias]) + list(waitq[alias])
        if load_signal == "count":
            return float(len(seqs))
        if load_signal == "chars":
            return float(sum(s.req.prompt_chars for s in seqs))
        # "work": remaining service iterations (oracle)
        total = 0
        for s in seqs:
            if s.prefill_iters or s.decode_iters:
                total += s.remaining_iters
            else:  # still queued: estimate from a cold prefill + its output
                total += math.ceil(s.req.prompt_tokens / sc.prefill_chunk_tokens)
                total += s.req.output_tokens
        return float(total)

    def _schedule_step(alias: str, t: float) -> None:
        if not step_scheduled[alias] and (active[alias] or waitq[alias]):
            step_scheduled[alias] = True
            heapq.heappush(heap, (t, next(order), _STEP, alias))

    async def _route(req: _Req) -> str:
        # Inject the chosen load signal into each member so the REAL scorer uses it.
        for alias in members:
            managed[alias].machine.inflight = int(round(_member_signal(alias)))
        route_key = f"sess-{req.session}" if strategy == "sticky" else None
        prefix_blocks = req.blocks if strategy == "cache_aware" else None
        chosen = await mgr._select_member(
            "chat", mgr.pools["chat"], set(), route_key, prefix_blocks
        )
        assert chosen is not None
        return chosen.entry.alias

    last_event_t = 0.0
    while heap:
        t, _, kind, payload = heapq.heappop(heap)
        last_event_t = t
        if kind == _ARRIVAL:
            req = cast(_Req, payload)
            alias = await _route(req)
            waitq[alias].append(_Seq(req=req))
            _schedule_step(alias, t)
            continue

        # _STEP for a member.
        alias = cast(str, payload)
        step_scheduled[alias] = False
        # Admit queued sequences into the batch (continuous batching).
        while waitq[alias] and len(active[alias]) < sc.member_slots:
            seq = waitq[alias].popleft()
            hit = caches[alias].hit_fraction(seq.req.blocks)
            hit_fracs.append(hit)
            uncached = max(1, int(round((1.0 - hit) * seq.req.prompt_tokens)))
            prefill_tokens_total += uncached
            seq.prefill_iters = max(1, math.ceil(uncached / sc.prefill_chunk_tokens))
            seq.decode_iters = seq.req.output_tokens
            caches[alias].add(seq.req.blocks)  # KV blocks created during prefill
            active[alias].append(seq)
        if not active[alias]:
            continue
        n = len(active[alias])
        dur = sc.iter_base_ms + sc.iter_per_seq_ms * n
        busy_ms[alias] += dur
        done_time = t + dur
        finished: list[_Seq] = []
        for seq in active[alias]:
            if not seq.prefill_done:
                seq.prefill_iters -= 1
                if seq.prefill_iters <= 0:
                    seq.prefill_done = True
                    seq.ttft_ms = done_time - seq.req.arrival
                    ttfts.append(seq.ttft_ms)
            else:
                seq.decode_iters -= 1
                if seq.decode_iters <= 0:
                    finished.append(seq)
        for seq in finished:
            active[alias].remove(seq)
            served_counts[alias] += 1
            completions += 1
            if mgr.cache_router is not None and seq.req.blocks:
                await mgr.cache_router.record("chat", seq.req.blocks, alias)
        _schedule_step(alias, done_time)

    await mgr.shutdown()
    ttfts.sort()
    load_values = list(served_counts.values())
    makespan_ms = last_event_t  # DES runs entirely in milliseconds
    throughput = (completions / (makespan_ms / 1000.0)) if makespan_ms > 0 else 0.0
    utils = {a: (busy_ms[a] / makespan_ms if makespan_ms > 0 else 0.0) for a in members}
    label = strategy if strategy != "cache_aware" else f"cache_aware β={beta:g}"
    return {
        "label": label,
        "strategy": strategy,
        "beta": beta,
        "load_signal": load_signal,
        "hit_rate": statistics.mean(hit_fracs) if hit_fracs else 0.0,
        "ttft_p50": _pct(ttfts, 50),
        "ttft_p95": _pct(ttfts, 95),
        "ttft_p99": _pct(ttfts, 99),
        "throughput": throughput,
        "util_max": max(utils.values()) if utils else 0.0,
        "util_mean": statistics.mean(utils.values()) if utils else 0.0,
        "prefill_tokens": prefill_tokens_total,
        "prefill_tokens_per_req": (prefill_tokens_total / completions) if completions else 0.0,
        "load_stdev": statistics.pstdev(load_values) if len(load_values) > 1 else 0.0,
        "load_spread": f"{min(load_values)}-{max(load_values)}",
        "load_min": min(load_values),
        "load_max": max(load_values),
    }


async def run_one(
    sc: Scenario, strategy: str, beta: float = 0.5, load_signal: str = "count"
) -> dict:
    """Public entry point (used by the regression tests): run one strategy on a
    scenario and return its metrics row. Deterministic for a fixed scenario."""
    rng = random.Random(sc.seed)
    requests = _build_workload(sc, rng)
    with tempfile.TemporaryDirectory() as td:
        mgr = _make_manager(Path(td), sc, strategy, beta)
        return await _run_strategy(mgr, sc, strategy, beta, requests, load_signal)


def _pct(sorted_vals: list[float], pct: int) -> float:
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, round((pct / 100) * (len(sorted_vals) - 1))))
    return sorted_vals[k]


def _format_table(rows: list[dict], first_col: str = "strategy") -> str:
    header = (
        f"| {first_col} | hit rate | TTFT p50 | p95 | p99 (ms) | "
        "work/req | throughput (req/s) | max util | load spread |"
    )
    sep = "|---|---|---|---|---|---|---|---|---|"
    lines = [header, sep]
    for r in rows:
        lines.append(
            f"| {r['label']} | {r['hit_rate'] * 100:.1f}% | {r['ttft_p50']:.0f} | "
            f"{r['ttft_p95']:.0f} | {r['ttft_p99']:.0f} | "
            f"{r.get('prefill_tokens_per_req', 0.0):.0f} | {r['throughput']:.1f} | "
            f"{r['util_max'] * 100:.0f}% | {r['load_spread']} |"
        )
    return "\n".join(lines)


async def _scenario_strategies(sc: Scenario, beta: float, load_signal: str) -> list[dict]:
    rng = random.Random(sc.seed)
    requests = _build_workload(sc, rng)
    rows = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for strategy in STRATEGIES:
            mgr = _make_manager(tmp, sc, strategy, beta)
            rows.append(await _run_strategy(mgr, sc, strategy, beta, requests, load_signal))
    return rows


async def _scenario_beta_sweep(
    sc: Scenario, betas: list[float], load_signal: str = "count"
) -> list[dict]:
    """least_busy + sticky baselines + cache_aware across β for one scenario.

    ``least_busy`` (the tail-latency-best strategy) is the reference the β selection
    rule compares against; ``sticky`` is kept for context (the default strategy).
    A single shared workload is replayed across all rows so only routing differs.
    """
    rng = random.Random(sc.seed)
    requests = _build_workload(sc, rng)
    rows = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for strat in ("least_busy", "sticky"):
            mgr = _make_manager(tmp, sc, strat, 0.0)
            rows.append(await _run_strategy(mgr, sc, strat, 0.0, requests, load_signal))
        for beta in betas:
            mgr = _make_manager(tmp, sc, "cache_aware", beta)
            rows.append(await _run_strategy(mgr, sc, "cache_aware", beta, requests, load_signal))
    return rows


# --- load calibration + capacity (ADR 0018 follow-up) -----------------------

#: Arrival-rate bisection bounds (requests/second) for calibration + capacity.
_RATE_LO, _RATE_HI = 0.25, 60.0

#: Target max-utilization load levels for the utilization sweep (``--sweep``).
SWEEP_TARGET_UTILS: tuple[float, ...] = (0.50, 0.70, 0.85, 0.95)

#: SLO on TTFT p95 (ms) for the capacity bisection. Chosen as a common interactive
#: first-token budget that sits ~an order of magnitude above the unloaded batched
#: prefill latency (~6-15 ms in this service model) and above the bursty service
#: floor, so "capacity" measures the onset of *queueing* delay, not the service
#: floor. Override with ``--slo-ms``.
SLO_TTFT_P95_MS: float = 200.0


async def calibrate_rate(
    sc: Scenario,
    target_util: float,
    *,
    strategy: str = "least_busy",
    beta: float = 0.5,
    load_signal: str = "count",
    iters: int = 16,
) -> float:
    """Bisection: arrival rate at which ``strategy`` reaches ~``target_util`` max
    utilization. Defaults to ``least_busy`` (the balanced strategy) as the common
    load reference, so at each sweep level every strategy sees the *same* offered
    load. Deterministic for a fixed scenario seed."""
    lo, hi = _RATE_LO, _RATE_HI
    for _ in range(iters):
        mid = (lo + hi) / 2
        row = await run_one(replace(sc, arrival_rate=mid), strategy, beta, load_signal)
        if row["util_max"] < target_util:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


async def capacity_for(
    sc: Scenario,
    strategy: str,
    beta: float = 0.5,
    *,
    slo_ms: float = SLO_TTFT_P95_MS,
    load_signal: str = "count",
    iters: int = 16,
) -> float:
    """Bisection: the max arrival rate (req/s) that keeps TTFT p95 ≤ ``slo_ms`` for
    ``strategy`` — the strategy's **sustained capacity** under the SLO. p95 is
    monotone non-decreasing in arrival rate here, so a bisection is well-defined."""
    lo, hi = _RATE_LO, _RATE_HI
    for _ in range(iters):
        mid = (lo + hi) / 2
        row = await run_one(replace(sc, arrival_rate=mid), strategy, beta, load_signal)
        if row["ttft_p95"] <= slo_ms:
            lo = mid
        else:
            hi = mid
    return lo


async def run_sweep(
    sc: Scenario,
    *,
    targets: tuple[float, ...] = SWEEP_TARGET_UTILS,
    beta: float = 0.5,
    load_signal: str = "count",
    calib_iters: int = 16,
) -> list[dict]:
    """Utilization sweep: for each target max-util level (calibrated via least_busy),
    run all four strategies at that **same** offered load and return metric rows
    tagged with ``target_util`` + the calibrated ``arrival_rate``. Deterministic."""
    rows: list[dict] = []
    for u in targets:
        rate = await calibrate_rate(sc, u, load_signal=load_signal, iters=calib_iters)
        sc_u = replace(sc, arrival_rate=rate)
        for strategy in STRATEGIES:
            row = dict(await run_one(sc_u, strategy, beta, load_signal))
            row["target_util"] = u
            row["arrival_rate"] = rate
            rows.append(row)
    return rows


# --- β selection rule (ADR 0018 follow-up) ----------------------------------

#: Max fraction by which cache_aware's p95 TTFT may exceed **least_busy's** (the
#: tail-latency-best strategy) at a moderate, non-saturated load, in ANY scenario.
BETA_P95_TOLERANCE = 0.10
#: Max low-pressure (long_lived) served-count spread ratio (max/min) allowed.
BETA_BALANCE_RATIO = 1.25
#: Load level (target max utilization) at which the rule is evaluated — moderate,
#: not saturated, so a strategy's steady-state tail is measured, not pure queueing.
BETA_SELECT_TARGET_UTIL = 0.85


def select_beta(matrix: dict[str, list[dict]], betas: list[float]) -> dict:
    """Apply the ADR 0018-follow-up β selection rule to a scenario→rows matrix.

    The rule reflects what ``cache_aware`` is *for* — cache reuse / prefill-work
    saved — while refusing to pay a tail-latency penalty versus the tail-best
    strategy (``least_busy``) at a realistic (non-saturated) load:

    > Among the swept β, keep those that (a) hold the low-pressure ``long_lived``
    > served-count spread ≤ ``BETA_BALANCE_RATIO`` and (b) in NO scenario are worse
    > than ``least_busy`` on p95 TTFT by more than ``BETA_P95_TOLERANCE`` **at
    > ≤85% utilization**. Among the survivors pick the **smallest β** — the least
    > load spreading, hence the most cache reuse / least prefill work (the metric
    > the feature exists to improve).

    ``least_busy`` is the reference, so the matrix MUST carry a ``least_busy`` row
    per scenario and SHOULD be built at the moderate load (see ``_build_matrix``).
    The trace also carries mean prefill work/req so the pick is auditable.
    """
    least_busy = {
        scn: next(r for r in rows if r["strategy"] == "least_busy") for scn, rows in matrix.items()
    }
    candidates = []
    trace = []
    for beta in betas:
        rows_by_scn = {
            scn: next(r for r in rows if r["strategy"] == "cache_aware" and r["beta"] == beta)
            for scn, rows in matrix.items()
        }
        ll = rows_by_scn["long_lived"]
        balance = ll["load_max"] / max(1, ll["load_min"])
        balance_ok = balance <= BETA_BALANCE_RATIO
        worst_ratio = 0.0
        for scn, r in rows_by_scn.items():
            lb = least_busy[scn]["ttft_p95"]
            if lb > 0:
                worst_ratio = max(worst_ratio, (r["ttft_p95"] - lb) / lb)
        p95_ok = worst_ratio <= BETA_P95_TOLERANCE
        mean_work = statistics.mean(
            r.get("prefill_tokens_per_req", 0.0) for r in rows_by_scn.values()
        )
        eligible = balance_ok and p95_ok
        trace.append(
            {
                "beta": beta,
                "balance_ratio": balance,
                "balance_ok": balance_ok,
                "worst_p95_vs_least_busy": worst_ratio,
                "p95_ok": p95_ok,
                "mean_work_per_req": mean_work,
                "eligible": eligible,
            }
        )
        if eligible:
            candidates.append(beta)
    chosen = min(candidates) if candidates else None
    return {"chosen": chosen, "trace": trace}


async def _build_matrix(
    betas: list[float],
    load_signal: str = "count",
    *,
    target_util: float | None = BETA_SELECT_TARGET_UTIL,
) -> dict[str, list[dict]]:
    """Scenario→rows matrix (least_busy + sticky baselines + cache_aware per β).

    When ``target_util`` is set (the default), each scenario's arrival rate is first
    calibrated (via least_busy) to that moderate load, so the β selection rule and
    the headline matrix measure steady-state tails rather than pure saturation
    queueing. Pass ``target_util=None`` to use each scenario's native arrival rate.
    """
    matrix: dict[str, list[dict]] = {}
    for sc in SCENARIOS.values():
        if target_util is not None:
            rate = await calibrate_rate(sc, target_util, load_signal=load_signal)
            sc = replace(sc, arrival_rate=rate)
        matrix[sc.name] = await _scenario_beta_sweep(sc, betas, load_signal)
    return matrix


async def _main_async(args: argparse.Namespace) -> int:
    betas = DEFAULT_BETAS if not args.smoke else [0.0, 0.25, 1.0]

    if args.select_beta:
        matrix = await _build_matrix(betas, args.load_signal)
        decision = select_beta(matrix, betas)
        print("\nADR 0018 β selection rule (queueing model, moderate load)\n")
        print(
            f"Rule: evaluated at ~{BETA_SELECT_TARGET_UTIL * 100:.0f}% max utilization "
            f"(not saturation). Keep β with long_lived served spread ≤ "
            f"{BETA_BALANCE_RATIO:g}× AND no scenario worse than LEAST_BUSY (the "
            f"tail-best strategy) on p95 by >{BETA_P95_TOLERANCE * 100:.0f}%; then pick "
            "the smallest eligible β (most cache reuse / least prefill work).\n"
        )
        print(
            "| β | long_lived spread | balance ok | worst p95 vs least_busy | "
            "p95 ok | mean work/req | eligible |"
        )
        print("|---|---|---|---|---|---|---|")
        for row in decision["trace"]:
            print(
                f"| {row['beta']:g} | {row['balance_ratio']:.2f}× | "
                f"{'yes' if row['balance_ok'] else 'no'} | "
                f"{row['worst_p95_vs_least_busy'] * 100:+.0f}% | "
                f"{'yes' if row['p95_ok'] else 'no'} | {row['mean_work_per_req']:.0f} | "
                f"{'YES' if row['eligible'] else 'no'} |"
            )
        print(f"\nSelected default β = {decision['chosen']}")
        print(f"Config default β  = {Settings().cache_aware_beta:g}")
        return 0

    if args.matrix:
        print(
            "\nCache-aware routing — scenario × β matrix (queueing model, moderate "
            f"~{BETA_SELECT_TARGET_UTIL * 100:.0f}% util; least_busy + sticky baselines)\n"
        )
        matrix = await _build_matrix(betas, args.load_signal)
        for sc in SCENARIOS.values():
            rows = matrix[sc.name]
            print(f"### {sc.name} — {sc.description}")
            print(
                f"(members={sc.members}, sessions={sc.sessions}, requests={sc.requests}, "
                f"system_prompts={sc.system_prompts}, cache_capacity={sc.cache_capacity}, "
                f"arrival_rate calibrated to ~{BETA_SELECT_TARGET_UTIL * 100:.0f}% util, "
                f"slots={sc.member_slots}, skew={sc.skew}, load_signal={args.load_signal})\n"
            )
            print(_format_table(rows, first_col="strategy"))
            print()
        decision = select_beta(matrix, betas)
        print(
            f"β selection rule picks: {decision['chosen']} "
            f"(config default {Settings().cache_aware_beta:g}). Run --select-beta for the trace."
        )
        return 0

    if args.sweep:
        targets = SWEEP_TARGET_UTILS if not args.smoke else (0.5, 0.9)
        scen_names = sorted(SCENARIOS) if not args.smoke else ["cross_session"]
        print(
            "\nUtilization sweep (queueing model) — all four strategies at matched "
            f"offered load; cache_aware at β={args.beta:g}\n"
        )
        for name in scen_names:
            base = SCENARIOS[name]
            if args.smoke:
                base = replace(base, members=3, sessions=40, requests=120)
            rows = await run_sweep(
                base,
                targets=targets,
                beta=args.beta,
                load_signal=args.load_signal,
                calib_iters=10 if args.smoke else 16,
            )
            print(f"### {name} — {base.description}")
            print(
                "Rows grouped by target max-utilization level (arrival rate calibrated "
                "via least_busy; 'util' is the achieved max utilization per strategy).\n"
            )
            by_level: dict[float, list[dict]] = {}
            for r in rows:
                by_level.setdefault(r["target_util"], []).append(r)
            for u, lvl_rows in by_level.items():
                rate = lvl_rows[0]["arrival_rate"]
                print(f"load ≈{u * 100:.0f}% util (λ={rate:.1f}/s):")
                print(_format_table(lvl_rows, first_col="strategy"))
                print()
        # Capacity: max sustained arrival rate under the p95 SLO (bisection).
        print(
            f"### Capacity — max sustained λ (req/s) with TTFT p95 ≤ SLO={args.slo_ms:g} ms "
            "(bisection per strategy)\n"
        )
        print(f"| scenario | round_robin | least_busy | sticky | cache_aware β={args.beta:g} |")
        print("|---|---|---|---|---|")
        for name in scen_names:
            base = SCENARIOS[name]
            if args.smoke:
                base = replace(base, members=3, sessions=40, requests=120)
            caps = {}
            for strat in STRATEGIES:
                caps[strat] = await capacity_for(
                    base,
                    strat,
                    args.beta,
                    slo_ms=args.slo_ms,
                    load_signal=args.load_signal,
                    iters=10 if args.smoke else 16,
                )
            print(
                f"| {name} | {caps['round_robin']:.1f} | {caps['least_busy']:.1f} | "
                f"{caps['sticky']:.1f} | {caps['cache_aware']:.1f} |"
            )
        print(
            "\n(Capacity is p95-SLO sustained load. cache_aware also reports lower "
            "prefill work/req in the sweep tables above — its distinct benefit when the "
            "bottleneck is prefill compute rather than batch slots.)"
        )
        return 0

    if args.capacity_sweep:
        base = SCENARIOS["long_lived"]
        caps = [16, 24, 32, 48, 64, 128, 2000]
        print("\nCache-aware vs sticky under a cache-capacity sweep (long_lived traffic)\n")
        print(
            f"| cache_capacity | sticky hit / p95 / thru | cache_aware β={args.beta:g} hit / p95 / thru |"
        )
        print("|---|---|---|")
        for cap in caps:
            sc = replace(base, cache_capacity=cap)
            st = await run_one(sc, "sticky")
            ca = await run_one(sc, "cache_aware", args.beta, args.load_signal)
            print(
                f"| {cap} | {st['hit_rate'] * 100:.1f}% / {st['ttft_p95']:.0f} / {st['throughput']:.1f} | "
                f"{ca['hit_rate'] * 100:.1f}% / {ca['ttft_p95']:.0f} / {ca['throughput']:.1f} |"
            )
        return 0

    # Default: one scenario, all strategies.
    sc = SCENARIOS[args.scenario]
    if args.smoke:
        sc = replace(sc, members=3, sessions=20, requests=60, arrival_rate=8.0)
    rows = await _scenario_strategies(sc, args.beta, args.load_signal)
    print(f"\nRouting benchmark (queueing model) — scenario '{sc.name}' at β={args.beta:g}")
    print(f"{sc.description}")
    print(
        f"(members={sc.members}, sessions={sc.sessions}, requests={sc.requests}, "
        f"system_prompts={sc.system_prompts}, cache_capacity={sc.cache_capacity}, "
        f"arrival_rate={sc.arrival_rate}/s, slots={sc.member_slots}, skew={sc.skew}, "
        f"load_signal={args.load_signal})\n"
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
        "--select-beta",
        action="store_true",
        help="apply the ADR 0018 β selection rule to the matrix and print the pick",
    )
    p.add_argument(
        "--capacity-sweep",
        action="store_true",
        help="sticky vs cache_aware across cache capacities (long_lived traffic)",
    )
    p.add_argument(
        "--sweep",
        action="store_true",
        help="utilization sweep: all 4 strategies at ~50/70/85/95%% util + SLO capacity",
    )
    p.add_argument(
        "--slo-ms",
        type=float,
        default=SLO_TTFT_P95_MS,
        help=f"TTFT p95 SLO (ms) for the --sweep capacity bisection (default {SLO_TTFT_P95_MS:g})",
    )
    p.add_argument(
        "--beta",
        type=float,
        default=Settings().cache_aware_beta,
        help="cache_aware β for single-scenario / sweep / matrix-default (default: config default)",
    )
    p.add_argument(
        "--load-signal",
        choices=LOAD_SIGNALS,
        default="count",
        help="load signal fed to the scorer (count=ADR0010; chars=prefill-work proxy; work=oracle)",
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
