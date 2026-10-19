"""Router eval harness (ADR 0011).

Two layers of coverage:

* **Metric math** — the RouteLLM primitives (``pgr``, ``frontier``, ``apgr``,
  ``cpt``, ``oracle_frontier``) are pure and are checked against hand-computed
  values, including the no-clamping and ``invalid`` behaviors.
* **Harness** — ``run_eval`` runs offline on the heuristic backend, the router's
  swept curve is distinct from (and bounded by) the oracle upper bound, and the
  ``invalid`` flag fires on a degenerate (no-gap) dataset.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import router_eval  # noqa: E402

# --- pure metric math (hand-computable) --------------------------------------


def test_pgr_is_raw_and_unclamped():
    # Exact gap recovery.
    assert router_eval.pgr(0.5, 0.2, 0.8) == pytest.approx(0.5)
    # acc above the ceiling -> PGR > 1, reported RAW (never clamped to 1.0).
    assert router_eval.pgr(1.0, 0.2, 0.8) == pytest.approx(4.0 / 3.0)
    assert router_eval.pgr(1.0, 0.2, 0.8) > 1.0
    # acc below weak -> negative PGR, reported raw.
    assert router_eval.pgr(0.1, 0.2, 0.8) < 0.0
    # Non-positive gap -> undefined.
    assert router_eval.pgr(0.5, 0.5, 0.5) is None
    assert router_eval.pgr(0.5, 0.8, 0.2) is None


def test_frontier_cummax_anchor_and_extend():
    raw = [(0.5, 0.3), (0.2, 0.6), (0.2, 0.4), (0.8, None)]
    f = router_eval.frontier(raw)
    # None dropped; best-per-cost + cummax; anchored at (0,0); extended to cost 1.
    assert f == [(0.0, 0.0), (0.2, 0.6), (0.5, 0.6), (1.0, 0.6)]


def test_apgr_and_cpt_trapezoid():
    pts = [(0.0, 0.0), (0.5, 1.0), (1.0, 1.0)]
    assert router_eval.apgr(pts) == pytest.approx(0.75)
    assert router_eval.cpt(pts, 0.5) == pytest.approx(0.5)
    assert router_eval.cpt(pts, 0.8) == pytest.approx(0.5)
    # Curve never reaches the target -> None (surfaced as invalid upstream).
    assert router_eval.cpt([(0.0, 0.0), (1.0, 0.4)], 0.8) is None


def test_oracle_frontier_hand_computed():
    # weak="w" (cost 1); "s" (cost 3). A is weak-correct; B,C need "s".
    cost_of = {"w": 1, "s": 3}
    items = [
        {"acceptable": ["w", "s"], "cheapest_capable": "w"},
        {"acceptable": ["s"], "cheapest_capable": "s"},
        {"acceptable": ["s"], "cheapest_capable": "s"},
    ]
    pts = router_eval.oracle_frontier(items, cost_of)
    # r_weak=1/3, r_strong=1, gap=2/3. Upgrades cost 2 each, span=2.
    assert pts[0] == (0.0, 0.0)
    assert pts[1][0] == pytest.approx(1.0 / 3.0)  # norm cost after one upgrade
    assert pts[1][1] == pytest.approx(0.5)  # PGR 0.5
    assert pts[2][0] == pytest.approx(2.0 / 3.0)
    assert pts[2][1] == pytest.approx(1.0)
    f = router_eval.frontier(pts)
    assert router_eval.apgr(f) == pytest.approx(2.0 / 3.0)
    assert router_eval.cpt(f, 0.5) == pytest.approx(1.0 / 3.0)
    assert router_eval.cpt(f, 0.8) == pytest.approx(2.0 / 3.0)


def test_quality_ceiling_is_a_true_upper_bound():
    items = [
        {"acceptable": ["tiny"]},
        {"acceptable": ["coder", "reasoner"]},
        {"acceptable": ["vizard"]},
    ]
    names = {"tiny", "coder", "reasoner", "vizard"}
    # Every prompt has an acceptable model -> ceiling 1.0 >= any router accuracy.
    assert router_eval.quality_ceiling(items, names) == pytest.approx(1.0)
    assert router_eval.response_quality(items, "tiny") == pytest.approx(1.0 / 3.0)


# --- harness end to end ------------------------------------------------------


def test_eval_runs_and_computes_sane_direct_metrics():
    m = router_eval.run_eval()
    assert m["backend"] == "heuristic"
    assert m["n"] >= 40
    for key in ("accuracy", "cheapest_capable_accuracy", "strong_call_fraction"):
        assert 0.0 <= m[key] <= 1.0, f"{key} out of range: {m[key]}"
    assert m["accuracy"] > 0.5
    assert 1.0 <= m["mean_cost"] <= 5.0
    assert 1.0 <= m["oracle_mean_cost"] <= 5.0


def test_pgr_not_clamped_and_well_defined():
    rl = router_eval.run_eval()["routellm"]
    # Ceiling is a real upper bound (r_strong >= accuracy), so PGR is in range and
    # defined without the old clamping artifact. The improved ADR 0019 heuristic
    # reaches the oracle ceiling on the bundled set, so a genuine PGR == 1.0 is
    # allowed here (distinct from the old *clamped* 1.000 produced by a mis-defined
    # ceiling, which this test guards against via r_strong >= r_weak + gap_valid).
    assert rl["r_strong"] >= rl["r_weak"]
    assert rl["gap_valid"] is True
    pgr = rl["pgr_router"]
    assert pgr["valid"] is True
    assert 0.0 <= pgr["value"] <= 1.0
    # Operating point lies on the router's own curve.
    op = rl["operating_point"]
    assert [op["cost"], pgr["value"]] in [list(p) for p in rl["router_curve"]["frontier"]] or any(
        p["weights_cost"] == op["weights_cost"] and p["pgr"] == pgr["value"]
        for p in rl["router_curve"]["points"]
    )


def test_router_curve_is_swept_and_distinct_from_oracle():
    rl = router_eval.run_eval()["routellm"]
    pts = rl["router_curve"]["points"]
    assert len(pts) >= 10  # >=10 sweep points per ADR 0011
    assert rl["sweep_knob"] == "router.weights.cost"
    # Sweeping the knob actually moves the router: not all points identical.
    assert len({(p["cost"], p["pgr"]) for p in pts}) > 1
    rc = rl["router_curve"]
    ob = rl["oracle_upper_bound"]
    # The router's curve and the oracle frontier are different objects.
    assert rc["frontier"] != ob["frontier"]
    # Oracle is a cost-optimal upper bound: dominates the router curve.
    assert ob["apgr"]["value"] >= rc["apgr"]["value"]
    assert ob["cpt_50"]["value"] <= rc["cpt_50"]["value"]
    assert ob["cpt_80"]["value"] <= rc["cpt_80"]["value"]


def test_invalid_flag_fires_on_degenerate_no_gap_dataset(tmp_path):
    # Every prompt is answerable by the weak model -> r(M_w) == r(M_s) == 1.0,
    # the gap is zero, and every RouteLLM metric must be marked invalid (not clamped).
    ds = tmp_path / "degenerate.jsonl"
    ds.write_text(
        "\n".join(
            '{"prompt": "hi there friend", "flags": {}, '
            '"acceptable": ["tiny", "coder", "reasoner"], '
            '"cheapest_capable": "tiny", "task": "chat"}'
            for _ in range(3)
        ),
        encoding="utf-8",
    )
    m = router_eval.run_eval(ds)
    rl = m["routellm"]
    assert rl["gap_valid"] is False
    assert rl["pgr_router"]["valid"] is False
    assert rl["pgr_router"]["reason"]
    for col in ("router_curve", "oracle_upper_bound"):
        for metric in ("apgr", "cpt_50", "cpt_80"):
            cell = rl[col][metric]
            assert cell["valid"] is False
            assert cell["value"] is None
            assert cell["reason"]
    # Markdown surfaces the invalidity rather than a saturated number.
    md = router_eval.render_markdown(m)
    assert "invalid" in md


def test_eval_markdown_report_renders():
    m = router_eval.run_eval()
    md = router_eval.render_markdown(m)
    assert "Router evaluation report" in md
    assert "APGR" in md and "CPT(50%)" in md
    assert "Router curve" in md and "Oracle upper bound" in md
    assert "Accuracy by task type" in md


def test_eval_dataset_labels_reference_known_models():
    items = router_eval.load_dataset(router_eval.DEFAULT_DATASET)
    known = {m["alias"] for m in router_eval.DEFAULT_REGISTRY["models"]}
    for item in items:
        assert item["cheapest_capable"] in known
        for a in item["acceptable"]:
            assert a in known
