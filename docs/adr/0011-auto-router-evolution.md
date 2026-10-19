# ADR 0011: Auto-router evolution — cascade, latency-budget, privacy pinning, eval harness

- Status: Proposed (draft)
- Date: 2026-10-18
- Supersedes: none (extends ADR 0004)
- Related: `docs/auto-router.md`, ADR 0007 (usage store); prior art: RouteLLM,
  local-llm-router, vLLM Semantic Router

## Context

The `auto` router does single-shot classification (heuristic or tiny GBNF LLM).
We already saw the 0.5B mis-route a code task where the heuristic was correct, and
we have no way to *measure* routing quality. Peers add proven levers: cascade
(cheap→escalate), latency-budget spill, privacy pinning, and learned routers.

## Decision (proposed)

Evolve `auto` along four axes, all opt-in and data-driven:
1. **Cascade mode**: try the cheapest capable model first; escalate to a stronger
   one on a quality signal (confidence/length/verifier). RouteLLM: ~86% of queries
   never need the top model.
2. **Latency-budget fallback**: per-route `latency_budget_ms`; if the chosen
   model's expected latency (from health/usage stats) exceeds it, pick a faster
   capable one.
3. **Privacy / placement pinning (fail-closed)**: tag requests (or routes) as
   sensitive/region-bound and restrict them to allow-listed local backends;
   **error rather than spill** — a hard wall, matching our security posture.
4. **Eval harness**: a labeled request set + CPT/APGR metrics (and an optional
   learned/embedding router backend) so router changes are measured, fed by the
   ADR 0007 decision log.

## Alternatives considered

- **Single-shot only (status quo)** — simple but leaves cost/quality on the table
  and can't be tuned with evidence.
- **Adopt RouteLLM wholesale** — heavier dependency; our GBNF/heuristic + a cascade
  mode + eval harness captures most of the value, model-agnostically.

## Consequences

- Cascade means some requests hit two models (latency/cost trade — gated by mode).
- Privacy pinning adds a request-classification + allow-list check before routing.
- The eval harness makes "did routing improve?" answerable instead of vibes.
