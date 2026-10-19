"""Deterministic regression test for the retuned ``cache_aware`` default β (ADR
0010 follow-up).

Drives ``scripts/bench_routing.py``'s named scenarios at the **config default**
``beta`` (``Settings().cache_aware_beta``) and pins the behavior the default was
chosen for: under cache pressure and cross-session prefix sharing, ``cache_aware``
must be at least as good as ``sticky`` on both prefix hit rate and TTFT p95; and on
the low-pressure scenario it must respect the documented load-balance bound that
the default-β selection rule is built on. These are fast, seeded, and run the real
``ModelManager`` selection logic (not a reimplementation).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from broker.config import Settings

# Import the bench script as a module (scripts/ is not a package). Register it in
# sys.modules before exec so its @dataclass can resolve its own __module__.
_BENCH_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bench_routing.py"
_spec = importlib.util.spec_from_file_location("bench_routing", _BENCH_PATH)
assert _spec is not None and _spec.loader is not None
bench = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bench
_spec.loader.exec_module(bench)

#: The shipped default — the whole point of the retune. If someone changes the
#: config default, this test re-evaluates the new value against the same bar.
DEFAULT_BETA = Settings().cache_aware_beta


def test_default_beta_is_the_retuned_value():
    # Pins the documented default so a silent revert to 1.0 fails loudly.
    assert DEFAULT_BETA == 0.25


@pytest.mark.parametrize("scenario_name", ["cache_pressure", "cross_session"])
async def test_cache_aware_default_beta_beats_sticky(scenario_name):
    """At the default β, cache_aware ≥ sticky on hit rate AND TTFT p95 for the two
    scenarios the feature exists for."""
    sc = bench.SCENARIOS[scenario_name]
    sticky = await bench.run_one(sc, "sticky")
    cache_aware = await bench.run_one(sc, "cache_aware", DEFAULT_BETA)

    assert cache_aware["hit_rate"] >= sticky["hit_rate"] - 1e-9, (
        f"{scenario_name}: cache_aware hit {cache_aware['hit_rate']:.3f} "
        f"< sticky {sticky['hit_rate']:.3f}"
    )
    assert cache_aware["ttft_p95"] <= sticky["ttft_p95"] + 1e-9, (
        f"{scenario_name}: cache_aware p95 {cache_aware['ttft_p95']:.0f} "
        f"> sticky {sticky['ttft_p95']:.0f}"
    )


async def test_cache_pressure_default_beta_substantially_lifts_hit_rate():
    """Guard the headline claim: under cache pressure the default β roughly doubles
    the hit rate vs sticky (not merely ties)."""
    sc = bench.SCENARIOS["cache_pressure"]
    sticky = await bench.run_one(sc, "sticky")
    cache_aware = await bench.run_one(sc, "cache_aware", DEFAULT_BETA)
    assert cache_aware["hit_rate"] >= 1.5 * sticky["hit_rate"]


async def test_default_beta_respects_low_pressure_balance_bound():
    """The selection rule: at the default β the low-pressure (long_lived) pool stays
    balanced — no member handles more than 1.25× the least-loaded member. This is
    what rules out the smaller β values (0.0/0.1) that give higher raw hit rate."""
    sc = bench.SCENARIOS["long_lived"]
    row = await bench.run_one(sc, "cache_aware", DEFAULT_BETA)
    spread_ratio = row["load_max"] / max(1, row["load_min"])
    assert spread_ratio <= 1.25, f"long_lived spread {row['load_spread']} ratio {spread_ratio:.2f}"
