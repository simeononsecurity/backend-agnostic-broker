"""Deterministic regression + correctness tests for the queueing-model routing
benchmark (ADR 0018).

ADR 0010's bench modeled TTFT as pure routing savings (no queueing); ADR 0018
replaces that with a discrete-event simulation of continuous-batching servers, so
load imbalance produces real queueing delay. These tests:

* pin the **re-validated default β** (``Settings().cache_aware_beta``) and the
  behaviour the selection rule chose it for (balance bound + no-worse-than-sticky
  p95), and
* validate the **queueing model itself** (TTFT percentile ordering, bounded
  utilisation, uniform load ⇒ symmetric queues, zero-cache ⇒ cache_aware ≈
  least_busy, β monotonicity), and
* unit-test the **β selection rule** on synthetic matrices.

All runs are fast, seeded, and drive the real ``ModelManager`` selection logic.
"""

from __future__ import annotations

import importlib.util
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


# --- default-β regression ---------------------------------------------------


def test_default_beta_is_the_retuned_value():
    # Pins the ADR 0018 default; a silent revert to 0.25 (or 1.0) fails loudly.
    assert DEFAULT_BETA == 0.5


@pytest.mark.parametrize("scenario_name", ["cache_pressure", "cross_session"])
async def test_cache_aware_default_beta_beats_sticky(scenario_name):
    """At the default β, cache_aware ≥ sticky on hit rate AND TTFT p95 (incl.
    queueing) for the two scenarios the feature exists for."""
    sc = bench.SCENARIOS[scenario_name]
    sticky = await bench.run_one(sc, "sticky")
    cache_aware = await bench.run_one(sc, "cache_aware", DEFAULT_BETA)
    assert cache_aware["hit_rate"] >= sticky["hit_rate"] - 1e-9, (
        f"{scenario_name}: hit {cache_aware['hit_rate']:.3f} < sticky {sticky['hit_rate']:.3f}"
    )
    assert cache_aware["ttft_p95"] <= sticky["ttft_p95"] + 1e-9, (
        f"{scenario_name}: p95 {cache_aware['ttft_p95']:.0f} > sticky {sticky['ttft_p95']:.0f}"
    )


async def test_cache_pressure_default_beta_substantially_lifts_hit_rate():
    """Under cache pressure the default β materially lifts hit rate vs sticky."""
    sc = bench.SCENARIOS["cache_pressure"]
    sticky = await bench.run_one(sc, "sticky")
    cache_aware = await bench.run_one(sc, "cache_aware", DEFAULT_BETA)
    assert cache_aware["hit_rate"] >= 1.3 * sticky["hit_rate"]


async def test_default_beta_respects_low_pressure_balance_bound():
    """The selection rule's balance bound: at the default β the low-pressure
    (long_lived) pool stays balanced — max/min served ≤ 1.25×. This is exactly what
    rules out β=0.25 once queueing is modeled (it hot-spots to ~1.35×)."""
    sc = bench.SCENARIOS["long_lived"]
    row = await bench.run_one(sc, "cache_aware", DEFAULT_BETA)
    spread_ratio = row["load_max"] / max(1, row["load_min"])
    assert spread_ratio <= bench.BETA_BALANCE_RATIO, (
        f"long_lived spread {row['load_spread']} ratio {spread_ratio:.2f}"
    )


async def test_beta_025_violates_balance_bound_under_queueing():
    """The ADR 0018 headline: the old default β=0.25, re-measured under the queueing
    model, now breaks the 1.25× balance bound on the low-pressure scenario."""
    sc = bench.SCENARIOS["long_lived"]
    row = await bench.run_one(sc, "cache_aware", 0.25)
    spread_ratio = row["load_max"] / max(1, row["load_min"])
    assert spread_ratio > bench.BETA_BALANCE_RATIO


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


# --- β selection rule (unit tests on synthetic matrices) --------------------


def _row(strategy, beta, p95, load_min, load_max, hit=0.5):
    return {
        "strategy": strategy,
        "beta": beta,
        "ttft_p95": p95,
        "load_min": load_min,
        "load_max": load_max,
        "hit_rate": hit,
    }


def _matrix(long_lived_rows, other_p95):
    """Build a 4-scenario matrix: long_lived uses the given rows (which carry the
    balance signal); the other three just carry p95 vs a fixed sticky baseline."""
    m = {"long_lived": [_row("sticky", 0.0, 1000, 100, 100)] + long_lived_rows}
    for scn in ("cache_pressure", "cross_session", "skewed_bursty"):
        rows = [_row("sticky", 0.0, 1000, 100, 100)]
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
    # All betas beat sticky on p95 in the other scenarios.
    other = {0.0: 400, 0.25: 380, 0.5: 300, 1.0: 250}
    matrix, _ = _matrix(ll, other)
    decision = bench.select_beta(matrix, betas)
    assert decision["chosen"] == 0.5  # smallest eligible


def test_select_beta_excludes_p95_regressions():
    betas = [0.0, 0.25, 0.5, 1.0]
    ll = [
        _row("cache_aware", 0.0, 500, 100, 110),
        _row("cache_aware", 0.25, 500, 100, 110),
        _row("cache_aware", 0.5, 500, 100, 110),
        _row("cache_aware", 1.0, 500, 100, 110),
    ]
    # β=0.25 is >10% worse than sticky (1000) on p95 in the other scenarios.
    other = {0.0: 900, 0.25: 1200, 0.5: 900, 1.0: 800}
    matrix, _ = _matrix(ll, other)
    decision = bench.select_beta(matrix, betas)
    # 0.0 is eligible and smallest; 0.25 excluded by the p95 bound.
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
    """Integration: build the real matrix (reduced β set for speed) and confirm the
    documented rule rejects β=0.25 and selects the shipped config default."""
    betas = [0.25, 0.5]
    matrix = await bench._build_matrix(betas)
    decision = bench.select_beta(matrix, betas)
    trace = {t["beta"]: t for t in decision["trace"]}
    assert trace[0.25]["eligible"] is False  # balance bound broken under queueing
    assert decision["chosen"] == DEFAULT_BETA


# --- smoke ------------------------------------------------------------------


def test_smoke_cli_runs(tmp_path):
    import subprocess

    out = subprocess.run(
        [sys.executable, str(_BENCH_PATH), "--smoke"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert "queueing model" in out.stdout
