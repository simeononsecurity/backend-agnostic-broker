# ADR 0009: Routing reliability & horizontal scale — retries/cooldowns + optional shared state

- Status: Proposed (draft)
- Date: 2026-10-18
- Supersedes: none
- Related: ADR 0003 (pools/sticky/health), `docs/roadmap.md`; prior art: LiteLLM
  retries/cooldowns + Redis

## Context

Two gaps vs production gateways:
1. **Reliability** — failover is a single hop within a pool; there is no
   configurable retry with backoff, and a failed member isn't temporarily taken
   out of rotation (cooldown).
2. **Horizontal scale** — sticky map, in-flight counters, health, and (future)
   rate limits all live in-process, so running multiple broker replicas behind a
   load balancer gives inconsistent routing/limits.

## Decision (proposed)

- **Retries + cooldowns**: configurable retry count with exponential backoff
  across pool members; mark a member in `cooldown` for a window after repeated
  failures so routing skips it, then half-opens (circuit-breaker style).
- **Optional shared state via Redis**: when `BROKER_REDIS_URL` is set, store the
  sticky map, per-member in-flight/health, cooldowns, and rate-limit counters in
  Redis so replicas share one view. Absent the URL, behavior is exactly today's
  in-process state (no new dependency by default).

## Alternatives considered

- **In-process only (status quo)** — simplest, but caps us at one replica.
- **Always require Redis** — rejected; breaks the zero-dependency self-contained
  default.
- **Sticky sessions at the external LB** — helps session affinity but not shared
  rate limits/budgets/cooldowns.

## Consequences

- A state abstraction (in-process | Redis) behind the manager's routing hooks.
- Redis becomes a soft dependency only when multi-replica is wanted.
- Retries must respect the no-silent-cross-model rule: retry within the pool/model,
  never silently to a different model.
