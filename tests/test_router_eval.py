"""Router eval harness (ADR 0011): runs offline on the heuristic backend and
asserts the metrics are computed and sane. Serves as the CI gate for the harness.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import router_eval  # noqa: E402


def test_eval_runs_and_computes_sane_metrics():
    m = router_eval.run_eval()
    assert m["backend"] == "heuristic"
    assert m["n"] >= 40  # labeled dataset has >=40 prompts across task types
    for key in (
        "accuracy",
        "cheapest_capable_accuracy",
        "pgr_router",
        "apgr",
        "cpt_50",
        "cpt_80",
        "strong_call_fraction",
    ):
        assert 0.0 <= m[key] <= 1.0, f"{key} out of range: {m[key]}"
    # A working router should clearly beat always-weak on this labeled set.
    assert m["accuracy"] > 0.5
    assert m["accuracy"] > m["r_weak"]
    # Costs are within the 1..5 tier range.
    assert 1.0 <= m["mean_cost"] <= 5.0
    assert 1.0 <= m["oracle_mean_cost"] <= 5.0
    # CPT is monotonic: reaching a higher PGR needs at least as many strong calls.
    assert m["cpt_50"] <= m["cpt_80"]


def test_eval_markdown_report_renders():
    m = router_eval.run_eval()
    md = router_eval.render_markdown(m)
    assert "Router evaluation report" in md
    assert "APGR" in md and "CPT(50%)" in md
    assert "Accuracy by task type" in md


def test_eval_dataset_labels_reference_known_models():
    items = router_eval.load_dataset(router_eval.DEFAULT_DATASET)
    known = {m["alias"] for m in router_eval.DEFAULT_REGISTRY["models"]}
    for item in items:
        assert item["cheapest_capable"] in known
        for a in item["acceptable"]:
            assert a in known
