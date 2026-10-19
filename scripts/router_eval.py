#!/usr/bin/env python3
"""Router evaluation harness (ADR 0011).

Measures the ``auto`` router against a labeled prompt set instead of vibe-checking
it. Runs the **heuristic** backend offline (CI-able, no GPU, no network) and
optionally the llama.cpp LLM backend when a GGUF is available. Computes:

* ``accuracy`` — fraction of prompts routed to an *acceptable* model.
* ``cheapest_capable_accuracy`` — fraction routed to the single cheapest capable
  model (the cost-optimal pick).
* ``mean_cost`` vs ``oracle_mean_cost`` — average ``cost_tier`` of the router's
  picks vs the cheapest-capable labels.
* RouteLLM metrics — **PGR / APGR / CPT(50%), CPT(80%)** against an *oracle*
  cost–quality frontier derived from the labels (weak = globally cheapest model,
  strong = globally most expensive model; the strong-call fraction is swept by
  greedily granting strong to the prompts that gain the most accuracy). See
  ``docs/research/adr-0011-auto-router-evolution.md`` for the exact definitions
  and why this deterministic, multi-model adaptation is faithful to RouteLLM.

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
from broker.registry import Registry  # noqa: E402

DEFAULT_DATASET = Path(__file__).resolve().parent.parent / "tests" / "data" / "router_eval.jsonl"

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


def _percentile_curve(
    gains: list[int], r_weak: float, r_strong: float
) -> list[tuple[float, float]]:
    """(strong_fraction, PGR) points from the oracle frontier (greedy by gain)."""
    n = len(gains)
    gains_sorted = sorted(gains, reverse=True)  # route highest-gain prompts to strong first
    denom = (r_strong - r_weak) or 1.0
    pts = [(0.0, 0.0)]
    acc = r_weak
    for k in range(1, n + 1):
        acc += gains_sorted[k - 1] / n
        pgr = (acc - r_weak) / denom
        pts.append((k / n, max(0.0, min(1.0, pgr))))
    return pts


def _apgr(points: list[tuple[float, float]]) -> float:
    """Trapezoidal integral of PGR over the strong-call fraction (RouteLLM APGR)."""
    area = 0.0
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        area += (x1 - x0) * (y0 + y1) / 2.0
    return area


def _cpt(points: list[tuple[float, float]], target: float) -> float:
    """Minimum strong-call fraction to reach PGR >= target (RouteLLM CPT(x%))."""
    for frac, pgr in points:
        if pgr >= target:
            return frac
    return 1.0


def run_eval(
    dataset_path: str | Path = DEFAULT_DATASET,
    registry_path: str | None = None,
    backend: str = "heuristic",
) -> dict:
    """Run the router over the labeled set and return a metrics dict."""
    items = load_dataset(dataset_path)
    mgr = _build_manager(registry_path, backend)
    router = mgr.router
    if router is None:
        raise RuntimeError("router is disabled; set router_backend != 'off'")

    # cost_tier per public model name (no pools in the eval registry).
    cost_of = {m.public_name: m.entry.cost_tier for m in mgr.models.values()}
    tiers = sorted(set(cost_of.values()))
    weak_model = min(cost_of, key=lambda k: (cost_of[k], k))
    strong_model = max(cost_of, key=lambda k: (cost_of[k], k))
    strong_tier = max(cost_of.values())

    n = len(items)
    hits = 0
    cheapest_hits = 0
    router_cost = 0.0
    oracle_cost = 0.0
    gains: list[int] = []
    weak_correct = 0
    strong_correct = 0
    strong_calls = 0
    by_task: dict[str, dict[str, int]] = {}
    rows = []

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
        wc = int(weak_model in acceptable)
        sc = int(strong_model in acceptable)
        weak_correct += wc
        strong_correct += sc
        gains.append(sc - wc)
        task = item.get("task", "other")
        bt = by_task.setdefault(task, {"n": 0, "hits": 0})
        bt["n"] += 1
        bt["hits"] += int(ok)
        rows.append({"prompt": item["prompt"][:48], "task": task, "chosen": chosen, "ok": ok})

    r_weak = weak_correct / n
    r_strong = strong_correct / n
    acc = hits / n
    denom = (r_strong - r_weak) or 1.0
    pgr_router = max(0.0, min(1.0, (acc - r_weak) / denom))
    curve = _percentile_curve(gains, r_weak, r_strong)

    return {
        "backend": router.backend.name,
        "dataset": str(dataset_path),
        "n": n,
        "models": cost_of,
        "cost_tiers": tiers,
        "weak_model": weak_model,
        "strong_model": strong_model,
        "accuracy": round(acc, 4),
        "cheapest_capable_accuracy": round(cheapest_hits / n, 4),
        "mean_cost": round(router_cost / n, 4),
        "oracle_mean_cost": round(oracle_cost / n, 4),
        "r_weak": round(r_weak, 4),
        "r_strong": round(r_strong, 4),
        "pgr_router": round(pgr_router, 4),
        "apgr": round(_apgr(curve), 4),
        "cpt_50": round(_cpt(curve, 0.50), 4),
        "cpt_80": round(_cpt(curve, 0.80), 4),
        "strong_call_fraction": round(strong_calls / n, 4),
        "by_task": {k: {**v, "accuracy": round(v["hits"] / v["n"], 4)} for k, v in by_task.items()},
        "rows": rows,
    }


def render_markdown(m: dict) -> str:
    lines = [
        "# Router evaluation report",
        "",
        f"- Backend: `{m['backend']}` · Dataset: `{m['dataset']}` · Prompts: {m['n']}",
        f"- Weak model: `{m['weak_model']}` (cheapest) · Strong model: "
        f"`{m['strong_model']}` (most expensive)",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Accuracy (chosen is acceptable) | {m['accuracy']:.3f} |",
        f"| Cheapest-capable accuracy | {m['cheapest_capable_accuracy']:.3f} |",
        f"| Mean cost_tier (router) | {m['mean_cost']:.3f} |",
        f"| Mean cost_tier (oracle / cheapest-capable) | {m['oracle_mean_cost']:.3f} |",
        f"| Weak-only accuracy r(Mw) | {m['r_weak']:.3f} |",
        f"| Strong-only accuracy r(Ms) | {m['r_strong']:.3f} |",
        f"| PGR (router operating point) | {m['pgr_router']:.3f} |",
        f"| APGR (oracle frontier) | {m['apgr']:.3f} |",
        f"| CPT(50%) strong-call fraction | {m['cpt_50']:.3f} |",
        f"| CPT(80%) strong-call fraction | {m['cpt_80']:.3f} |",
        f"| Router strong-call fraction | {m['strong_call_fraction']:.3f} |",
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
    args = ap.parse_args(argv)

    metrics = run_eval(args.dataset, args.registry, args.backend)
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
