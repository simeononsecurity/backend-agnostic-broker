# ADR 0011: Auto-router evolution — cascade, latency-budget, privacy pinning, eval harness

- Status: Accepted
- Date: 2026-10-18
- Supersedes: none (extends ADR 0004)
- Related: `docs/auto-router.md`, `docs/research/adr-0011-auto-router-evolution.md`,
  ADR 0004 (auto router), ADR 0007 (usage/decision store); prior art: RouteLLM,
  FrugalGPT, vLLM Semantic Router, LiteLLM tag routing, Portkey conditional routing.

## Context

The `auto` router does single-shot classification (heuristic or tiny GBNF LLM).
We observed the 0.5B classifier mis-route a code task to `tiny` where the
heuristic correctly picked `coder`, and we have **no way to measure** routing
quality — only anecdote. Meanwhile the broker already carries the substrate for
better routing: per-model `cost_tier` metadata (ADR 0004) and a per-request
latency/decision log (ADR 0007).

The research note surveys the proven levers. Three are production-standard and
dependency-free for us: **cascade** (cheapest-capable first, escalate on a signal
that inspects the actual answer — FrugalGPT), **latency-budget** routing (pick the
fastest capable model that meets an SLA, estimated from a rolling p50/p90 window —
LLMGateway/Nexus/Not Diamond), and **placement/residency pinning** enforced by the
gateway as a fail-closed allow-list (LiteLLM tag routing, Portkey conditional
routing). A fourth — a *learned* router (RouteLLM `mf`, vLLM Semantic Router's
BERT/LoRA, Not Diamond) — is too heavy a dependency for a local-first broker, but
its evaluation metrics (PGR/APGR/CPT) are exactly what we lack.

The tension: add real routing intelligence without (a) a new mandatory
dependency, (b) changing default `auto` behavior, or (c) weakening the
no-silent-fallback / fail-closed security invariants.

## Decision

We will evolve `auto` along four axes, **all opt-in and default-off**, so a
request with none of the new headers and a registry with none of the new fields
reproduces v0.4.0 behavior exactly.

1. **Cascade mode.** A registry `router.mode: single | cascade` (default
   `single`). In cascade mode the router emits a two-hop plan — the cheapest
   capable model first, then one stronger capable model — and the manager serves
   the first, inspects the *actual* response, and **escalates at most once** when a
   configurable signal fires: `finish_reason == "length"`, empty/too-short output,
   or a refusal regex (all data-driven in `router.cascade`, pluggable). Both hops
   are recorded in the decision log and the usage log, and the response carries
   `x-broker-route` showing the path (e.g. `tiny->coder`). Streaming cannot cheaply
   re-score a stream, so a streaming cascade request **falls back to single**
   (the classifier's best pick) with that reason documented and surfaced in the
   route header.

2. **Latency budget.** A per-request `x-broker-latency-budget-ms` header (and a
   `router.latency_budget_ms` / `BROKER_ROUTER_LATENCY_BUDGET_MS` default). The
   router estimates each candidate's latency from the ADR 0007 usage log
   (p90 over a recent window, plus a cold-start penalty when the model is not
   resident) and, if the chosen model's estimate exceeds the budget, picks the
   fastest capable candidate instead. It **never** violates the Stage-1 capability
   filter or placement.

3. **Placement / privacy pinning (fail-closed).** Per-model `placement` labels in
   the registry (e.g. `local`, `trusted`, `region:eu`). A request requirement from
   `x-broker-placement` (and a `BROKER_DEFAULT_PLACEMENT` default) filters `auto`
   candidates, **and** rejects an explicit model or pool whose members don't carry
   all required labels; pools keep only satisfying members. If nothing qualifies
   the broker returns a structured **`403 placement_denied`** and never spills to a
   non-compliant model. The requirement is set server-side (trusted header /
   config), mirroring LiteLLM's "tag set by the gateway, never the end client".

4. **Eval harness.** `scripts/router_eval.py` + a labeled set
   `tests/data/router_eval.jsonl` (≥40 prompts across task types with
   acceptable-model / cheapest-capable labels and quality tiers). It runs the
   **heuristic** backend offline (CI-able), optionally the LLM backend when a GGUF
   is present, and computes accuracy, cheapest-capable match, cost-weighting, and
   RouteLLM's **PGR / APGR / CPT(50%,80%)** against an oracle cost–quality frontier
   derived from the labels (the deterministic, >2-model adaptation of RouteLLM's
   definitions is documented in the research note and the script). It emits a
   markdown report; a pytest runs it on the heuristic backend and asserts the
   metrics are computed and in range.

No new mandatory dependency is introduced; cascade/latency/placement are pure
Python over existing data, and the eval harness is stdlib-only.

## Alternatives considered

- **Single-shot only (status quo)** — simple, but leaves the cost lever (cascade)
  and the latency/residency controls on the table, and keeps "did routing
  improve?" unanswerable.
- **Adopt a learned router wholesale** (RouteLLM `mf`, vLLM SR BERT/LoRA, Not
  Diamond) — best raw quality, but adds a trained model, preference data, and (for
  SR) a Rust/Candle + Envoy stack. Rejected as a dependency for a local-first
  broker; we adopt its *metrics* and *escalation* ideas instead.
- **Trained reliability scorer for cascade** (FrugalGPT's DistilBERT `g(q,a)`) —
  more accurate escalation, but needs labeled data and a model. Rejected in favor
  of cheap, pluggable boolean signals.
- **Full multi-hop / streaming cascade** — larger cost savings, but unbounded
  latency and no cheap way to re-score a live stream. Rejected: cap at one
  escalation, non-streaming only.
- **Soft placement (prefer, then spill)** — better availability, but violates
  residency guarantees. Rejected: placement is a hard, fail-closed wall.
- **Client-supplied placement** — simplest, but a client could downgrade its own
  residency. Rejected: requirement is server-side.

## Consequences

- **Positive** — cascade can serve most requests on the cheapest model and only
  pay for a stronger one when the answer is actually bad; latency budget respects
  SLAs; placement gives an auditable, fail-closed residency wall; the eval harness
  makes routing changes measurable (PGR/APGR/CPT) in CI.
- **Negative** — cascade doubles cost/latency on escalated requests; latency
  estimates are end-to-end (not TTFT) and need a warm usage log to be meaningful;
  placement adds a pre-filter on every request that carries a requirement.
- **Risks** — an over-eager refusal regex could escalate too often (mitigation:
  signals are opt-in and data-driven, defaults conservative); a misconfigured
  placement label could deny all traffic (mitigation: fail-closed is intentional
  and the 403 names the required labels); the harness's APGR/CPT are an adaptation
  of RouteLLM's two-model definitions (mitigation: the adaptation is documented in
  the research note and the script, and the raw accuracy/cost numbers are reported
  alongside).

## Confirmation

Implemented in `broker/router.py` (plan + signals + latency pick + placement
filter), `broker/manager.py` (cascade execution, placement enforcement for
explicit models/pools), `broker/registry.py` (`placement`, `router.mode`,
`router.cascade`, `router.latency_budget_ms`), `broker/usage.py`
(`latency_percentiles`), `broker/errors.py` (`PlacementDeniedError`),
`broker/config.py` (new settings), and `broker/app.py` (header parsing,
`x-broker-route`). Verified by:

- `tests/test_router_cascade.py` — cascade escalation on each signal,
  no-escalation pass-through, both hops recorded, streaming falls back to single.
- `tests/test_placement.py` — fail-closed `403 placement_denied` for `auto`,
  explicit model, and pool; pool member filtering; header parsing/validation.
- `tests/test_router_latency.py` — latency-budget picks a faster capable model;
  budget never overrides capability; header parsing/validation.
- `tests/test_router_eval.py` — the harness runs on the heuristic backend and
  produces sane, in-range accuracy/APGR/CPT metrics on `tests/data/router_eval.jsonl`.
- Existing `tests/test_router.py` is unchanged and green (default behavior
  preserved).

The eval result table from a CI run of the harness is reproduced in
`docs/auto-router.md`.
