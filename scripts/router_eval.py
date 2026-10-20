#!/usr/bin/env python3
"""Router evaluation harness (ADR 0011).

Measures the ``auto`` router against a labeled prompt set instead of vibe-checking
it. Runs the **heuristic** backend offline (CI-able, no GPU, no network) and
optionally the llama.cpp LLM backend when a GGUF is available.

Two families of numbers are reported:

* **Direct metrics** (always valid): ``accuracy`` (fraction routed to an
  *acceptable* model), ``cheapest_capable_accuracy`` (fraction routed to the
  single cheapest capable model), ``mean_cost`` vs ``oracle_mean_cost`` (average
  ``cost_tier``), per-task accuracy, and ``strong_call_fraction``.

* **RouteLLM metrics** — **PGR / APGR / CPT(50%), CPT(80%)** — computed two ways:

  1. From the **router's own cost–quality curve**: we sweep a real router knob
     (``router.weights.cost``, the cost-penalty weight) over ``DEFAULT_COST_SWEEP``
     (≥10 points) and, at each setting, re-route the whole set and record
     ``(normalized_mean_cost, PGR)``. This traces the router's *achievable*
     curve — the RouteLLM analogue for a deterministic, multi-model router that
     has no single continuous ``α``. ``apgr_router`` / ``cpt*_router`` come from
     this curve; the router's default-weights operating point is reported on it.
  2. From an **oracle cost–quality frontier** (``oracle_*``) — the *cost-optimal*
     upper bound derived from the labels (keep every weak-correct prompt on the
     cheapest model; upgrade each weak-wrong prompt to its cheapest acceptable
     model, cheapest fixes first). This is reported **separately and explicitly
     as an upper bound**, never under the bare name APGR.

**Quality reference (ceiling).** RouteLLM's two-model setup has a single strong
model ``M_s`` that is a genuine quality ceiling. No single concrete model here is
capable across every task type (e.g. ``reasoner`` cannot serve vision), so the
globally-most-expensive model is *not* a ceiling and using it produces
``r(M_s) < accuracy`` and a meaningless clamped ``PGR > 1``. We instead define the
strong reference as the **quality ceiling** ``r(M_s) = (1/|Q|) Σ_q max_m s(q, m)``
— the accuracy of a perfect (oracle) policy that always picks an acceptable model
when one exists. By construction ``r(M_s) ≥ r(any router)``, so ``PGR ∈ [0, 1]``
without clamping. The weak reference ``M_w`` stays the globally cheapest model.
See ``docs/research/adr-0011-auto-router-evolution.md`` for the exact definitions
and citations.

**No silent clamping.** ``PGR`` is reported raw. If the quality gap
``r(M_s) − r(M_w) ≤ 0``, or a value falls outside ``[0, 1]``, the affected metric
is marked ``invalid`` with a reason in both the markdown and JSON output rather
than being clamped to a saturated value.

Usage::

    python scripts/router_eval.py                       # heuristic, markdown to stdout
    python scripts/router_eval.py --format json         # metrics as JSON
    python scripts/router_eval.py --out report.md       # write markdown report
    python scripts/router_eval.py --backend llm         # use the GGUF classifier
    python scripts/router_eval.py --dataset my.jsonl    # custom labeled set

The dataset is JSONL; each line::

    {"prompt": "...", "flags": {"has_images": true, "has_tools": true,
     "needs_json": true, "max_tokens": 50000}, "acceptable": ["coder"],
     "cheapest_capable": "coder", "quality_tier": 3, "task": "code"}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Make ``import broker`` work when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broker.config import Settings  # noqa: E402
from broker.manager import ModelManager  # noqa: E402
from broker.registry import Registry, RouterWeights  # noqa: E402

DEFAULT_DATASET = Path(__file__).resolve().parent.parent / "tests" / "data" / "router_eval.jsonl"

# Cost-penalty weights swept to trace the router's own cost–quality curve. Low
# values let capability dominate (the router "spends up" → higher accuracy and
# cost); high values make the cheap-model bonus dominate (cheaper, lower
# accuracy). ≥10 points per ADR 0011. 1.0 is the default weight (operating point).
DEFAULT_COST_SWEEP: list[float] = [0.0, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 4.0, 8.0, 16.0, 32.0]

# Built-in evaluation catalogue. The dataset labels reference these aliases. It is
# a plain registry dict (fake backend, no runtime), so the harness is fully
# offline. Pass --registry to evaluate against your own catalogue instead.
DEFAULT_REGISTRY: dict[str, Any] = {
    "models": [
        {
            "alias": "tiny",
            "backend": "fake",
            "runtime_model_id": "t",
            "tags": ["general", "cheap", "fast", "chat", "creative"],
            "cost_tier": 1,
        },
        {
            "alias": "tooly",
            "backend": "fake",
            "runtime_model_id": "to",
            "tags": ["tools", "json"],
            "supports_tools": True,
            "cost_tier": 2,
        },
        {
            "alias": "coder",
            "backend": "fake",
            "runtime_model_id": "c",
            "tags": ["code", "reasoning"],
            "supports_tools": True,
            "cost_tier": 3,
        },
        {
            "alias": "vizard",
            "backend": "fake",
            "runtime_model_id": "v",
            "tags": ["vision"],
            "supports_vision": True,
            "cost_tier": 4,
        },
        {
            "alias": "longboi",
            "backend": "fake",
            "runtime_model_id": "l",
            "tags": ["long-context"],
            "context_limit": 200000,
            "cost_tier": 4,
        },
        {
            "alias": "reasoner",
            "backend": "fake",
            "runtime_model_id": "r",
            "tags": ["reasoning", "math", "general"],
            "cost_tier": 5,
        },
    ]
}


def load_dataset(path: str | Path) -> list[dict]:
    items = []
    for i, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{i}: invalid JSON: {exc}") from exc
    if not items:
        raise ValueError(f"no prompts in dataset {path}")
    return items


def _payload(item: dict) -> dict:
    flags = item.get("flags") or {}
    content: Any = item["prompt"]
    if flags.get("has_images"):
        content = [
            {"type": "text", "text": item["prompt"]},
            {"type": "image_url", "image_url": {"url": "x"}},
        ]
    payload: dict[str, Any] = {"messages": [{"role": "user", "content": content}]}
    if flags.get("has_tools"):
        payload["tools"] = [{"type": "function", "function": {"name": "f"}}]
    if flags.get("needs_json"):
        payload["response_format"] = {"type": "json_object"}
    if flags.get("max_tokens"):
        payload["max_tokens"] = int(flags["max_tokens"])
    return payload


def _build_manager(registry_path: str | None, backend: str) -> ModelManager:
    if registry_path:
        from broker.registry import load_registry

        reg = load_registry(registry_path, {"fake", "ollama", "vllm", "llamacpp", "strata"})
    else:
        reg = Registry.model_validate(DEFAULT_REGISTRY)
    settings = Settings(
        auth_enabled=False,
        api_keys=[],
        enabled_backends=["fake", "ollama", "vllm", "llamacpp", "strata"],
        router_backend=backend,
        health_probe_enabled=False,
    )
    mgr = ModelManager(reg, settings)
    mgr.validate_all()
    return mgr


# --- RouteLLM metric primitives (pure; unit-tested with hand-computed inputs) --


def _metric(value: float | None, valid: bool = True, reason: str | None = None) -> dict:
    """A RouteLLM metric cell: raw ``value`` plus a ``valid`` flag and ``reason``.

    ``value`` is reported **raw** (never clamped); ``valid`` is ``False`` when the
    number is undefined or out of range, with ``reason`` explaining why.
    """
    return {
        "value": (round(value, 4) if value is not None else None),
        "valid": valid,
        "reason": reason,
    }


def response_quality(items: list[dict], model: str) -> float:
    """``r(model)`` — fraction of prompts for which ``model`` is acceptable."""
    n = len(items)
    return sum(1 for it in items if model in set(it["acceptable"])) / n if n else 0.0


def quality_ceiling(items: list[dict], model_names: set[str]) -> float:
    """``r(M_s)`` — accuracy of a perfect oracle: fraction of prompts that have at
    least one acceptable model available. This is a true upper bound on the
    accuracy of any router over this catalogue, so ``PGR`` never exceeds 1."""
    n = len(items)
    if not n:
        return 0.0
    return sum(1 for it in items if set(it["acceptable"]) & model_names) / n


def pgr(acc: float, r_weak: float, r_strong: float) -> float | None:
    """Performance Gap Recovered, **raw / unclamped**. ``None`` when the gap is
    non-positive (metric undefined)."""
    denom = r_strong - r_weak
    if denom <= 0:
        return None
    return (acc - r_weak) / denom


def frontier(raw: list[tuple[float, float | None]]) -> list[tuple[float, float]]:
    """Monotone upper envelope of ``(cost, PGR)`` points over the unit cost axis.

    Drops ``None`` PGRs, keeps the best PGR per cost, makes PGR non-decreasing in
    cost (a higher-cost operating point can always match a lower-cost one), anchors
    the weak baseline ``(0, 0)`` and extends flat to ``cost = 1`` so every curve
    spans ``[0, 1]`` and is directly comparable.
    """
    best: dict[float, float] = {}
    for c, p in raw:
        if p is None:
            continue
        best[c] = max(best.get(c, p), p)
    pts: list[tuple[float, float]] = []
    run = 0.0
    for c, p in sorted(best.items()):
        run = max(run, p)
        pts.append((c, run))
    if not pts:
        return [(0.0, 0.0), (1.0, 0.0)]
    if pts[0][0] > 0.0:
        pts.insert(0, (0.0, 0.0))
    if pts[-1][0] < 1.0:
        pts.append((1.0, pts[-1][1]))
    return pts


def apgr(points: list[tuple[float, float]]) -> float:
    """Trapezoidal integral of PGR over the normalized-cost axis (RouteLLM APGR)."""
    area = 0.0
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        area += (x1 - x0) * (y0 + y1) / 2.0
    return area


def cpt(points: list[tuple[float, float]], target: float, eps: float = 1e-9) -> float | None:
    """Minimum normalized cost to reach PGR ≥ ``target`` (RouteLLM CPT(x%)).
    ``None`` when the curve never reaches the target. ``eps`` absorbs float dust
    at an exact PGR == target boundary."""
    for x, y in points:
        if y >= target - eps:
            return x
    return None


# --- curve construction -------------------------------------------------------


def oracle_frontier(items: list[dict], cost_of: dict[str, int]) -> list[tuple[float, float | None]]:
    """Cost-optimal ``(normalized_mean_cost, PGR)`` upper bound from the labels.

    Keep every weak-correct prompt on the globally cheapest model; upgrade each
    weak-wrong prompt to its cheapest acceptable model, cheapest fixes first.
    """
    n = len(items)
    tiers = sorted(set(cost_of.values()))
    weak_tier, strong_tier = tiers[0], tiers[-1]
    weak_model = min(cost_of, key=lambda k: (cost_of[k], k))
    span = (strong_tier - weak_tier) or 1.0
    model_names = set(cost_of)
    r_weak = response_quality(items, weak_model)
    r_strong = quality_ceiling(items, model_names)
    denom = r_strong - r_weak

    deltas: list[int] = []
    for it in items:
        acceptable = set(it["acceptable"])
        if weak_model in acceptable:
            continue  # already correct on the weak model; keep it cheap
        if not (acceptable & model_names):
            continue  # unsatisfiable prompt: oracle cannot fix it
        cheapest = it.get("cheapest_capable") or min(
            acceptable & model_names, key=lambda a: (cost_of[a], a)
        )
        deltas.append(cost_of.get(cheapest, strong_tier) - weak_tier)
    deltas.sort()

    total_cost = float(n * weak_tier)
    acc = r_weak
    pts: list[tuple[float, float | None]] = [(0.0, (0.0 if denom > 0 else None))]
    for d in deltas:
        acc += 1.0 / n
        total_cost += d
        norm = (total_cost / n - weak_tier) / span
        pts.append((norm, (pgr(acc, r_weak, r_strong))))
    return pts


def router_curve(
    router: Any,
    items: list[dict],
    cost_of: dict[str, int],
    base_weights: RouterWeights,
    r_weak: float,
    r_strong: float,
    sweep: list[float] = DEFAULT_COST_SWEEP,
) -> list[dict]:
    """Sweep the router's cost-penalty weight and record its own cost–quality
    curve: one ``{cost, pgr, accuracy, weights_cost}`` point per sweep value."""
    tiers = sorted(set(cost_of.values()))
    weak_tier, strong_tier = tiers[0], tiers[-1]
    span = (strong_tier - weak_tier) or 1.0
    n = len(items)
    pts: list[dict] = []
    for v in sweep:
        router.cfg.weights = base_weights.model_copy(update={"cost": v})
        router._cache.clear()  # cache key ignores weights; must clear per sweep
        hits = 0
        total_cost = 0.0
        for it in items:
            chosen = router.route(_payload(it)).chosen
            hits += int(chosen in set(it["acceptable"]))
            total_cost += cost_of.get(chosen, 0)
        acc = hits / n
        norm = (total_cost / n - weak_tier) / span
        p = pgr(acc, r_weak, r_strong)
        pts.append(
            {
                "weights_cost": v,
                "cost": round(norm, 4),
                "accuracy": round(acc, 4),
                "pgr": (round(p, 4) if p is not None else None),
            }
        )
    router.cfg.weights = base_weights
    router._cache.clear()
    return pts


def _curve_metrics(points: list[tuple[float, float]], valid: bool, reason: str | None) -> dict:
    """APGR + CPT(50/80) for a prebuilt frontier, honoring the gap-validity flag."""
    if not valid:
        return {
            "apgr": _metric(None, False, reason),
            "cpt_50": _metric(None, False, reason),
            "cpt_80": _metric(None, False, reason),
        }
    c50 = cpt(points, 0.50)
    c80 = cpt(points, 0.80)
    return {
        "apgr": _metric(apgr(points), True, None),
        "cpt_50": _metric(
            c50, c50 is not None, None if c50 is not None else "curve never reaches PGR>=0.50"
        ),
        "cpt_80": _metric(
            c80, c80 is not None, None if c80 is not None else "curve never reaches PGR>=0.80"
        ),
    }


def run_eval(
    dataset_path: str | Path = DEFAULT_DATASET,
    registry_path: str | None = None,
    backend: str = "heuristic",
    sweep: list[float] = DEFAULT_COST_SWEEP,
    preload: list[str] | None = None,
) -> dict:
    """Run the router over the labeled set and return a metrics dict.

    ``preload`` marks those aliases as already loaded (warm), so the
    ``prefer_loaded`` term is exercised the way it is in a live broker, where a
    model is almost always resident. Default: nothing loaded (cold).
    """
    items = load_dataset(dataset_path)
    mgr = _build_manager(registry_path, backend)
    for m in mgr.models.values():
        if m.entry.alias in (preload or []):
            m.machine.is_active = lambda: True  # type: ignore[method-assign]
    router = mgr.router
    if router is None:
        raise RuntimeError("router is disabled; set router_backend != 'off'")

    # cost_tier per public model name (no pools in the eval registry).
    cost_of = {m.public_name: m.entry.cost_tier for m in mgr.models.values()}
    tiers = sorted(set(cost_of.values()))
    weak_tier, strong_tier = tiers[0], tiers[-1]
    weak_model = min(cost_of, key=lambda k: (cost_of[k], k))
    most_expensive = max(cost_of, key=lambda k: (cost_of[k], k))
    model_names = set(cost_of)

    # Quality references: weak = cheapest model; strong = oracle quality ceiling.
    r_weak = response_quality(items, weak_model)
    r_strong = quality_ceiling(items, model_names)
    gap_valid = (r_strong - r_weak) > 0
    gap_reason = None if gap_valid else "no quality gap: r(M_s) <= r(M_w) (weak already at ceiling)"

    n = len(items)
    hits = 0
    cheapest_hits = 0
    router_cost = 0.0
    oracle_cost = 0.0
    strong_calls = 0
    by_task: dict[str, dict[str, int]] = {}
    rows = []

    base_weights = router.cfg.weights
    router._cache.clear()
    for item in items:
        acceptable = set(item["acceptable"])
        cheapest = item.get("cheapest_capable") or ""
        decision = router.route(_payload(item))
        chosen = decision.chosen
        ok = chosen in acceptable
        hits += int(ok)
        cheapest_hits += int(chosen == cheapest)
        router_cost += cost_of.get(chosen, 0)
        oracle_cost += cost_of.get(cheapest, 0)
        if cost_of.get(chosen, 0) >= strong_tier:
            strong_calls += 1
        task = item.get("task", "other")
        bt = by_task.setdefault(task, {"n": 0, "hits": 0})
        bt["n"] += 1
        bt["hits"] += int(ok)
        rows.append({"prompt": item["prompt"][:48], "task": task, "chosen": chosen, "ok": ok})

    acc = hits / n
    span = (strong_tier - weak_tier) or 1.0
    op_cost = (router_cost / n - weak_tier) / span
    pgr_op = pgr(acc, r_weak, r_strong)
    pgr_valid = gap_valid and pgr_op is not None and 0.0 <= pgr_op <= 1.0
    pgr_reason = gap_reason
    if gap_valid and pgr_op is not None and not (0.0 <= pgr_op <= 1.0):
        pgr_reason = f"raw PGR {pgr_op:.4f} outside [0,1]"

    # Router's own cost–quality curve (swept knob) and the oracle upper bound.
    r_pts_raw = router_curve(router, items, cost_of, base_weights, r_weak, r_strong, sweep)
    router_front = frontier([(p["cost"], p["pgr"]) for p in r_pts_raw])
    oracle_pts = oracle_frontier(items, cost_of)
    oracle_front = frontier(oracle_pts)

    router_metrics = _curve_metrics(router_front, gap_valid, gap_reason)
    oracle_metrics = _curve_metrics(oracle_front, gap_valid, gap_reason)

    routellm = {
        "quality_ceiling": "oracle: best acceptable model per prompt (perfect routing)",
        "weak_model": weak_model,
        "most_expensive_model": most_expensive,
        "cost_axis": "normalized_mean_cost_tier (0 = weak, 1 = most expensive)",
        "sweep_knob": "router.weights.cost",
        "sweep_values": list(sweep),
        "r_weak": round(r_weak, 4),
        "r_strong": round(r_strong, 4),
        "gap_valid": gap_valid,
        "pgr_router": _metric(pgr_op, pgr_valid, pgr_reason if not pgr_valid else None),
        "operating_point": {
            "weights_cost": base_weights.cost,
            "cost": round(op_cost, 4),
            "accuracy": round(acc, 4),
        },
        "router_curve": {
            "points": r_pts_raw,
            "frontier": [[round(x, 4), round(y, 4)] for x, y in router_front],
            **router_metrics,
        },
        "oracle_upper_bound": {
            "frontier": [[round(x, 4), round(y, 4)] for x, y in oracle_front],
            **oracle_metrics,
        },
    }

    return {
        "backend": router.backend.name,
        "dataset": str(dataset_path),
        "n": n,
        "models": cost_of,
        "cost_tiers": tiers,
        "weak_model": weak_model,
        "most_expensive_model": most_expensive,
        "accuracy": round(acc, 4),
        "cheapest_capable_accuracy": round(cheapest_hits / n, 4),
        "mean_cost": round(router_cost / n, 4),
        "oracle_mean_cost": round(oracle_cost / n, 4),
        "strong_call_fraction": round(strong_calls / n, 4),
        "routellm": routellm,
        "by_task": {k: {**v, "accuracy": round(v["hits"] / v["n"], 4)} for k, v in by_task.items()},
        "rows": rows,
    }


def _cell(m: dict) -> str:
    """Render a metric cell: the raw value, or ``invalid (reason)``."""
    if m["value"] is None or not m["valid"]:
        return f"invalid ({m['reason']})"
    return f"{m['value']:.3f}"


def render_markdown(m: dict) -> str:
    rl = m["routellm"]
    rc = rl["router_curve"]
    ob = rl["oracle_upper_bound"]
    op = rl["operating_point"]
    lines = [
        "# Router evaluation report",
        "",
        f"- Backend: `{m['backend']}` · Dataset: `{m['dataset']}` · Prompts: {m['n']}",
        f"- Weak model (M_w): `{rl['weak_model']}` (cheapest) · Quality ceiling (M_s): "
        f"oracle best-acceptable (perfect routing)",
        "",
        "## Direct metrics",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Accuracy (chosen is acceptable) | {m['accuracy']:.3f} |",
        f"| Cheapest-capable accuracy | {m['cheapest_capable_accuracy']:.3f} |",
        f"| Mean cost_tier (router) | {m['mean_cost']:.3f} |",
        f"| Mean cost_tier (oracle / cheapest-capable) | {m['oracle_mean_cost']:.3f} |",
        f"| Router strong-call fraction (top tier) | {m['strong_call_fraction']:.3f} |",
        "",
        "## RouteLLM metrics",
        "",
        f"- Weak-only accuracy r(M_w): **{rl['r_weak']:.3f}** · Quality ceiling r(M_s): "
        f"**{rl['r_strong']:.3f}** · gap valid: **{rl['gap_valid']}**",
        f"- Router operating point (default weights): cost **{op['cost']:.3f}**, "
        f"accuracy **{op['accuracy']:.3f}**, PGR **{_cell(rl['pgr_router'])}**",
        f"- Cost axis: {rl['cost_axis']} · Swept knob: `{rl['sweep_knob']}` "
        f"({len(rl['sweep_values'])} points)",
        "",
        "| Metric | Router curve | Oracle upper bound |",
        "|--------|--------------|--------------------|",
        f"| PGR (operating point) | {_cell(rl['pgr_router'])} | — |",
        f"| APGR | {_cell(rc['apgr'])} | {_cell(ob['apgr'])} |",
        f"| CPT(50%) | {_cell(rc['cpt_50'])} | {_cell(ob['cpt_50'])} |",
        f"| CPT(80%) | {_cell(rc['cpt_80'])} | {_cell(ob['cpt_80'])} |",
        "",
        "> APGR / CPT in the **Router curve** column come from re-routing the whole "
        "set while sweeping `router.weights.cost` (≥10 points) — they evaluate the "
        "router. The **Oracle upper bound** column is the cost-optimal frontier "
        "derived from the labels (a ceiling the router cannot beat), reported for "
        "reference only. PGR is raw/unclamped; `invalid` marks an undefined or "
        "out-of-range value with its reason.",
        "",
        "## Accuracy by task type",
        "",
        "| Task | Prompts | Accuracy |",
        "|------|---------|----------|",
    ]
    for task, v in sorted(m["by_task"].items()):
        lines.append(f"| {task} | {v['n']} | {v['accuracy']:.3f} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate the auto-router (ADR 0011).")
    ap.add_argument("--dataset", default=str(DEFAULT_DATASET))
    ap.add_argument("--registry", default=None, help="registry file (default: built-in eval set)")
    ap.add_argument("--backend", default="heuristic", choices=["heuristic", "llm", "auto"])
    ap.add_argument("--format", default="md", choices=["md", "json"])
    ap.add_argument("--out", default=None, help="write the report to this file")
    ap.add_argument(
        "--preload",
        default="",
        help="comma-separated aliases to treat as already loaded (warm-state eval)",
    )
    args = ap.parse_args(argv)

    preload = [a.strip() for a in args.preload.split(",") if a.strip()]
    metrics = run_eval(args.dataset, args.registry, args.backend, preload=preload)
    if args.format == "json":
        text = json.dumps({k: v for k, v in metrics.items() if k != "rows"}, indent=2)
    else:
        text = render_markdown(metrics)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
