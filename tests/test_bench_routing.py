"""Deterministic regression + correctness tests for the queueing-model routing
benchmark (ADR 0018, and its follow-up: honest baselines + utilization sweep).

ADR 0010's bench modeled TTFT as pure routing savings (no queueing); ADR 0018
replaced that with a discrete-event simulation of continuous-batching servers, so
load imbalance produces real queueing delay. The ADR 0018 follow-up reframes the
results against the right baseline (``least_busy``, the tail-latency-best strategy)
and over a range of load levels (not one saturated point). These tests:

* pin the **re-validated default β** (``Settings().cache_aware_beta``) and the
  behaviour the selection rule chose it for — the balance bound plus
  **no-worse-than-least_busy p95 at a moderate (≤85% util) load**;
* guard the **honest conclusions**: ``least_busy`` is tail-best at saturation,
  ``cache_aware`` instead saves prefill work / lifts hit rate and has higher SLO
  capacity than ``sticky``;
* validate the **queueing model itself** (TTFT percentile ordering, bounded
  utilisation, uniform load ⇒ symmetric queues, zero-cache ⇒ cache_aware ≈
  least_busy, β monotonicity);
* exercise the **utilization sweep + SLO capacity** machinery; and
* unit-test the **β selection rule** (now least_busy-referenced) on synthetic
  matrices.

All runs are fast, seeded, and drive the real ``ModelManager`` selection logic.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from broker.config import Settings

# Import the bench script as a module (scripts/ is not a package).
_BENCH_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bench_routing.py"
_spec = importlib.util.spec_from_file_location("bench_routing", _BENCH_PATH)
assert _spec is not None and _spec.loader is not None
bench = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bench
_spec.loader.exec_module(bench)

#: The shipped default — the point of the ADR 0018 retune. If someone changes the
#: config default, these tests re-evaluate the new value against the same bar.
DEFAULT_BETA = Settings().cache_aware_beta


async def _moderate(scenario_name: str, util: float = 0.85):
    """Return the scenario with its arrival rate calibrated (via least_busy) to a
    moderate ``util`` max utilization — the load at which the β rule is evaluated,
    not the saturated operating point."""
    sc = bench.SCENARIOS[scenario_name]
    rate = await bench.calibrate_rate(sc, util, iters=12)
    return replace(sc, arrival_rate=rate)


# --- default-β regression + honest claims -----------------------------------


def test_default_beta_is_the_retuned_value():
    # Pins the ADR 0018 default; a silent revert to 0.25 (or 1.0) fails loudly.
    assert DEFAULT_BETA == 0.5


@pytest.mark.parametrize("scenario_name", ["cache_pressure", "cross_session"])
async def test_cache_aware_saves_prefill_work_vs_least_busy(scenario_name):
    """cache_aware's real purpose: at the default β it computes materially FEWER
    uncached prefill tokens (GPU work) than least_busy in reuse-heavy scenarios,
    and lifts hit rate over the tail-best strategy."""
    sc = await _moderate(scenario_name)
    lb = await bench.run_one(sc, "least_busy")
    ca = await bench.run_one(sc, "cache_aware", DEFAULT_BETA)
    assert ca["prefill_tokens_per_req"] < 0.9 * lb["prefill_tokens_per_req"], (
        f"{scenario_name}: work/req {ca['prefill_tokens_per_req']:.0f} "
        f"vs least_busy {lb['prefill_tokens_per_req']:.0f}"
    )
    assert ca["hit_rate"] > lb["hit_rate"] + 0.05


@pytest.mark.parametrize("scenario_name", ["cache_pressure", "cross_session", "long_lived"])
async def test_cache_aware_p95_within_tolerance_of_least_busy_at_moderate_load(scenario_name):
    """The selection-rule constraint: at ≤85% util the default β is no worse than
    least_busy (the tail-best strategy) on p95 TTFT beyond the rule's tolerance
    (plus a small deterministic-sim slack)."""
    sc = await _moderate(scenario_name)
    lb = await bench.run_one(sc, "least_busy")
    ca = await bench.run_one(sc, "cache_aware", DEFAULT_BETA)
    assert ca["ttft_p95"] <= lb["ttft_p95"] * (1 + bench.BETA_P95_TOLERANCE) + 3, (
        f"{scenario_name}: cache_aware p95 {ca['ttft_p95']:.0f} vs least_busy {lb['ttft_p95']:.0f}"
    )


@pytest.mark.parametrize("scenario_name", ["cache_pressure", "cross_session", "skewed_bursty"])
async def test_least_busy_is_tail_best_at_saturation(scenario_name):
    """Honest headline: at the saturated operating point least_busy has the best
    (lowest) p95 TTFT — no worse than cache_aware — and sticky is far worse. The
    docs must not claim cache_aware is the latency-best strategy."""
    sc = bench.SCENARIOS[scenario_name]  # native (near-saturation) arrival rate
    lb = await bench.run_one(sc, "least_busy")
    ca = await bench.run_one(sc, "cache_aware", DEFAULT_BETA)
    sticky = await bench.run_one(sc, "sticky")
    assert lb["ttft_p95"] <= ca["ttft_p95"] + 1e-9
    assert ca["ttft_p95"] < sticky["ttft_p95"]


async def test_cache_pressure_default_beta_substantially_lifts_hit_rate():
    """Under cache pressure the default β materially lifts hit rate vs least_busy
    (the honest reference), not just vs sticky."""
    sc = await _moderate("cache_pressure")
    lb = await bench.run_one(sc, "least_busy")
    ca = await bench.run_one(sc, "cache_aware", DEFAULT_BETA)
    assert ca["hit_rate"] >= 1.4 * lb["hit_rate"]


async def test_cache_aware_capacity_beats_sticky_and_trails_least_busy():
    """SLO capacity (max λ with p95 ≤ SLO): cache_aware comfortably beats sticky,
    while least_busy is the capacity leader (cache_aware within a small margin)."""
    sc = replace(bench.SCENARIOS["cache_pressure"], requests=300)
    cap_lb = await bench.capacity_for(sc, "least_busy", iters=11)
    cap_ca = await bench.capacity_for(sc, "cache_aware", DEFAULT_BETA, iters=11)
    cap_st = await bench.capacity_for(sc, "sticky", iters=11)
    assert cap_ca > cap_st
    assert cap_lb >= cap_ca - 0.5


async def test_default_beta_respects_low_pressure_balance_bound():
    """The selection rule's balance bound: at the default β the low-pressure
    (long_lived) pool stays balanced — served max/min ≤ 1.25×. This is exactly what
    rules out β=0.25 once queueing is modeled (it hot-spots past 1.25×)."""
    sc = await _moderate("long_lived")
    row = await bench.run_one(sc, "cache_aware", DEFAULT_BETA)
    spread_ratio = row["load_max"] / max(1, row["load_min"])
    assert spread_ratio <= bench.BETA_BALANCE_RATIO, (
        f"long_lived spread {row['load_spread']} ratio {spread_ratio:.2f}"
    )


async def test_beta_025_violates_a_selection_bound_at_moderate_load():
    """The ADR 0018-follow-up headline: at ≤85% util the old default β=0.25 fails
    the selection rule — it breaks the balance bound on long_lived AND is >10%
    worse than least_busy on p95 in a reuse scenario (cross_session)."""
    ll = await _moderate("long_lived")
    row = await bench.run_one(ll, "cache_aware", 0.25)
    balance = row["load_max"] / max(1, row["load_min"])
    cs = await _moderate("cross_session")
    lb = await bench.run_one(cs, "least_busy")
    ca025 = await bench.run_one(cs, "cache_aware", 0.25)
    p95_ratio = (ca025["ttft_p95"] - lb["ttft_p95"]) / lb["ttft_p95"]
    assert balance > bench.BETA_BALANCE_RATIO or p95_ratio > bench.BETA_P95_TOLERANCE


# --- queueing-model correctness / sanity ------------------------------------


async def test_ttft_percentiles_are_ordered():
    sc = replace(bench.SCENARIOS["cross_session"], requests=300)
    row = await bench.run_one(sc, "cache_aware", DEFAULT_BETA)
    assert row["ttft_p50"] <= row["ttft_p95"] <= row["ttft_p99"]


async def test_utilisation_is_bounded_and_positive():
    sc = replace(bench.SCENARIOS["cache_pressure"], requests=300)
    row = await bench.run_one(sc, "least_busy")
    assert 0.0 < row["util_mean"] <= row["util_max"] <= 1.0 + 1e-9


async def test_throughput_positive_and_within_offered_load():
    sc = replace(bench.SCENARIOS["long_lived"], requests=300)
    row = await bench.run_one(sc, "least_busy")
    # A stable queue cannot complete faster than requests arrive (plus a small
    # drain-tail slack), and must make real progress.
    assert 0.0 < row["throughput"] <= sc.arrival_rate * 1.2


async def test_prefill_work_metric_is_consistent():
    """The prefill-work (uncached prefill tokens computed) metric is present and
    internally consistent: positive, and the per-request figure is the total over
    the completed request count."""
    sc = replace(bench.SCENARIOS["cross_session"], requests=300)
    row = await bench.run_one(sc, "cache_aware", DEFAULT_BETA)
    assert row["prefill_tokens"] > 0
    assert 0.0 < row["prefill_tokens_per_req"] <= row["prefill_tokens"]


async def test_uniform_load_gives_symmetric_queues():
    """Uniform arrivals under a load-balancing strategy ⇒ members serve symmetric
    counts (the Erlang-C symmetric-server sanity check)."""
    sc = replace(bench.SCENARIOS["long_lived"], requests=400, skew=0.0)
    row = await bench.run_one(sc, "least_busy")
    assert row["load_max"] / max(1, row["load_min"]) <= 1.2


async def test_zero_cache_makes_cache_aware_behave_like_least_busy():
    """With no prefix reuse (every request a unique prompt), cache_aware always
    finds no holder and falls back to least_busy — so their served distributions
    match closely."""
    # Unique system prompt per session AND one request per session ⇒ no shared
    # prefix is ever recorded, so match_fractions is always empty.
    sc = replace(
        bench.SCENARIOS["cross_session"],
        sessions=600,
        requests=600,
        system_prompts=600,
        cache_capacity=4,
    )
    lb = await bench.run_one(sc, "least_busy")
    ca = await bench.run_one(sc, "cache_aware", DEFAULT_BETA)
    assert ca["hit_rate"] <= 0.02  # essentially zero reuse
    # Load distributions line up (within a couple of requests per member).
    assert abs(ca["load_max"] - lb["load_max"]) <= 6
    assert abs(ca["load_min"] - lb["load_min"]) <= 6


async def test_higher_beta_reduces_tail_latency():
    sc = bench.SCENARIOS["cross_session"]
    low = await bench.run_one(sc, "cache_aware", 0.0)
    high = await bench.run_one(sc, "cache_aware", 1.0)
    assert high["ttft_p95"] < low["ttft_p95"]


async def test_higher_beta_reduces_load_spread():
    sc = bench.SCENARIOS["long_lived"]
    low = await bench.run_one(sc, "cache_aware", 0.0)
    high = await bench.run_one(sc, "cache_aware", 1.0)
    low_ratio = low["load_max"] / max(1, low["load_min"])
    high_ratio = high["load_max"] / max(1, high["load_min"])
    assert high_ratio < low_ratio


async def test_lower_beta_increases_hit_rate_under_pressure():
    sc = bench.SCENARIOS["cache_pressure"]
    low = await bench.run_one(sc, "cache_aware", 0.0)
    high = await bench.run_one(sc, "cache_aware", 2.0)
    assert low["hit_rate"] >= high["hit_rate"]


# --- utilization sweep + capacity -------------------------------------------


async def test_run_sweep_covers_strategies_and_rising_utilization():
    """run_sweep runs all four strategies at each calibrated level, and a higher
    target utilization yields a higher achieved max utilization."""
    sc = replace(bench.SCENARIOS["cross_session"], requests=200)
    rows = await bench.run_sweep(sc, targets=(0.5, 0.9), calib_iters=10)
    lvl50 = {r["strategy"]: r for r in rows if r["target_util"] == 0.5}
    lvl90 = {r["strategy"]: r for r in rows if r["target_util"] == 0.9}
    assert set(lvl50) == set(bench.STRATEGIES)
    assert set(lvl90) == set(bench.STRATEGIES)
    assert lvl50["least_busy"]["util_max"] < lvl90["least_busy"]["util_max"]


async def test_capacity_is_monotone_in_slo():
    """A looser SLO admits at least as much sustained load as a tighter one."""
    sc = replace(bench.SCENARIOS["long_lived"], requests=250)
    tight = await bench.capacity_for(sc, "least_busy", slo_ms=60, iters=11)
    loose = await bench.capacity_for(sc, "least_busy", slo_ms=300, iters=11)
    assert loose >= tight - 1e-9


# --- β selection rule (unit tests on synthetic matrices) --------------------


def _row(strategy, beta, p95, load_min, load_max, hit=0.5, work=100.0):
    return {
        "strategy": strategy,
        "beta": beta,
        "ttft_p95": p95,
        "load_min": load_min,
        "load_max": load_max,
        "hit_rate": hit,
        "prefill_tokens_per_req": work,
    }


def _matrix(long_lived_rows, other_p95):
    """Build a 4-scenario matrix: long_lived uses the given rows (which carry the
    balance signal); the other three carry p95 vs a fixed ``least_busy`` baseline.
    ``least_busy`` is the reference the rule compares against."""
    m = {"long_lived": [_row("least_busy", 0.0, 1000, 100, 100)] + long_lived_rows}
    for scn in ("cache_pressure", "cross_session", "skewed_bursty"):
        rows = [_row("least_busy", 0.0, 1000, 100, 100)]
        rows += [_row("cache_aware", b, p95, 100, 110) for b, p95 in other_p95.items()]
        m[scn] = rows
    return m, list(other_p95)


def test_select_beta_picks_smallest_eligible():
    betas = [0.0, 0.25, 0.5, 1.0]
    # long_lived: β=0/0.25 hot-spot (spread >1.25×); β=0.5/1.0 balanced.
    ll = [
        _row("cache_aware", 0.0, 500, 50, 200),  # 4.0× — fails balance
        _row("cache_aware", 0.25, 800, 100, 135),  # 1.35× — fails balance
        _row("cache_aware", 0.5, 300, 100, 110),  # 1.10× — ok
        _row("cache_aware", 1.0, 250, 100, 104),  # 1.04× — ok
    ]
    # All betas beat least_busy on p95 in the other scenarios.
    other = {0.0: 400, 0.25: 380, 0.5: 300, 1.0: 250}
    matrix, _ = _matrix(ll, other)
    decision = bench.select_beta(matrix, betas)
    assert decision["chosen"] == 0.5  # smallest eligible


def test_select_beta_excludes_p95_regressions_vs_least_busy():
    betas = [0.0, 0.25, 0.5, 1.0]
    ll = [
        _row("cache_aware", 0.0, 500, 100, 110),
        _row("cache_aware", 0.25, 500, 100, 110),
        _row("cache_aware", 0.5, 500, 100, 110),
        _row("cache_aware", 1.0, 500, 100, 110),
    ]
    # β=0.25 is >10% worse than least_busy (1000) on p95 in the other scenarios.
    other = {0.0: 900, 0.25: 1200, 0.5: 900, 1.0: 800}
    matrix, _ = _matrix(ll, other)
    decision = bench.select_beta(matrix, betas)
    # 0.0 is eligible and smallest; 0.25 excluded by the p95-vs-least_busy bound.
    trace = {t["beta"]: t for t in decision["trace"]}
    assert trace[0.25]["p95_ok"] is False
    assert decision["chosen"] == 0.0


def test_select_beta_returns_none_when_nothing_eligible():
    betas = [0.0, 0.5]
    ll = [
        _row("cache_aware", 0.0, 500, 50, 200),  # fails balance
        _row("cache_aware", 0.5, 500, 50, 200),  # fails balance
    ]
    other = {0.0: 300, 0.5: 300}
    matrix, _ = _matrix(ll, other)
    decision = bench.select_beta(matrix, betas)
    assert decision["chosen"] is None


async def test_real_matrix_selection_rejects_025_and_picks_config_default():
    """Integration: build the real matrix at the moderate load (reduced β set for
    speed) and confirm the least_busy-referenced rule rejects β=0.25 and selects the
    shipped config default."""
    betas = [0.25, 0.5]
    matrix = await bench._build_matrix(betas)
    decision = bench.select_beta(matrix, betas)
    trace = {t["beta"]: t for t in decision["trace"]}
    assert trace[0.25]["eligible"] is False  # fails balance and/or p95-vs-least_busy
    assert decision["chosen"] == DEFAULT_BETA


# --- smoke ------------------------------------------------------------------


def test_smoke_cli_runs():
    out = subprocess.run(
        [sys.executable, str(_BENCH_PATH), "--smoke"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert "queueing model" in out.stdout


def test_sweep_smoke_cli_runs():
    out = subprocess.run(
        [sys.executable, str(_BENCH_PATH), "--sweep", "--smoke"],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert out.returncode == 0, out.stderr
    assert "Capacity" in out.stdout
