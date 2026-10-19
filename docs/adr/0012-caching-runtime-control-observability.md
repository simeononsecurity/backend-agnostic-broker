# ADR 0012: Caching, runtime control, and tracing/UI

- Status: Proposed (draft)
- Date: 2026-10-18
- Supersedes: none
- Related: `docs/roadmap.md`; prior art: LiteLLM caching, llama-swap runtime
  control + UI

## Context

Three independent ops/experience gaps vs peers, grouped here as a roadmap draft
(each may split into its own ADR when picked up):

1. **Response caching** — no cache today; repeated prompts re-run inference.
   LiteLLM/OpenRouter show large latency/cost wins from exact (and optional
   semantic) caching.
2. **Runtime control** — no way to unload a model or reload the registry without a
   restart; llama-swap exposes manual unload + list-running + profiles.
3. **Observability depth** — we emit Prometheus + `/status`; no distributed
   tracing, and no built-in UI/playground.

## Decision (proposed)

- **Exact response cache**: opt-in, keyed by a hash of (model, normalized
  messages, sampling params); honor `stream`/`store` semantics; TTL + max size;
  backed by the ADR 0009 state abstraction (in-process or Redis). Semantic cache is
  a later, separate decision.
- **Runtime control endpoints**: `POST /admin/models/unload[/:id]`, list-running,
  and **registry hot-reload** (re-read + diff without dropping in-flight requests),
  all behind auth/admin scope.
- **OpenTelemetry tracing**: optional spans around route→load→inference, gated by
  `tracing_enabled`; keep Prometheus. A web UI/playground is optional and lowest
  priority.

## Alternatives considered

- **No cache** — simplest; leaves the biggest repeat-latency/cost win unclaimed.
- **Restart to reload config** — operationally painful for registry edits.
- **Metrics only** — fine for counts, weak for per-request latency breakdowns.

## Consequences

- Caching must never serve across auth/tenant boundaries or cache errors; correct
  invalidation is the main risk.
- Admin endpoints widen the control surface — must be auth-gated and audited.
- Each item is independently shippable; this draft records the direction.
