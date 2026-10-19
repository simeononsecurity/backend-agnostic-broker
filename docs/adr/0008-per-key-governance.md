# ADR 0008: Per-key governance — rate limits, virtual keys, budgets

- Status: Proposed (draft)
- Date: 2026-10-18
- Supersedes: none
- Related: ADR 0007 (usage store), `docs/roadmap.md`; prior art: LiteLLM virtual
  keys + budgets

## Context

Today auth is a flat allow-list of bearer keys, and the only quotas are global +
per-model concurrency (429 on saturation). Multi-tenant and cost-controlled
deployments need per-caller identity, request/token rate limits, and spend caps —
LiteLLM's core governance value.

## Decision (proposed)

Introduce **virtual keys**: each key has an id, optional label/tenant, and limits
(rpm, tpm, daily/monthly token or cost budget). Enforce in the request pipeline
*before* dispatch: identity → rate-limit (rpm/tpm) → budget check → route. Reuse
the ADR 0007 usage store for spend/tokens; expose per-key usage via `/usage`.
Keys and limits come from config/registry (no secrets in VCS); cost uses a
per-model price map.

## Alternatives considered

- **Delegate to an external API gateway** (Kong/Envoy/APISIX) — viable for big
  deployments; keep broker-native limits for the self-contained story.
- **Concurrency-only (status quo)** — insufficient for fairness/cost control.

## Consequences

- New enforcement stage + key store; must stay fast (cache in-memory, see ADR 0009
  for shared state across replicas).
- Cost accuracy depends on a maintained price map and token counts (streaming
  token counts are currently approximate — see ADR 0007).
