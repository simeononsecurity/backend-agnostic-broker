# ADR 0010: Cache-aware pool routing (prefix affinity + load-aware scoring)

- Status: Proposed (draft)
- Date: 2026-10-18
- Supersedes: none (extends ADR 0003)
- Related: `docs/roadmap.md`; prior art: vLLM production-stack & llm-d (KV/prefix/
  load-aware routing)

## Context

Our sticky routing pins a *session* to a pool member — a coarse proxy for cache
locality. Production stacks route on actual cache state: send a request to the
replica that already holds its prompt **prefix** (reusing KV cache) while avoiding
hot-spotting. llm-d reports ~3× throughput and ~2× faster TTFT vs round-robin from
prefix-aware routing. This is our single biggest throughput/latency lever for pools.

## Decision (proposed)

Add a `cache_aware` route strategy for pools:
- **Prefix affinity**: hash a normalized prompt prefix (e.g. system + leading
  messages) and prefer the member that recently served that prefix bucket.
- **Load-aware scoring**: pick by `score = matched_prefix_fraction − β·relative_load`
  (after the ADR 0003 health/capability filter), so a cache-warm member yields to a
  cold-but-idle one once it's sufficiently loaded. `β` is the single tunable.
- Use real backend load signals where available (vLLM `/metrics` queue/KV
  utilization) in addition to our in-flight counter.

Sticky remains the default; `cache_aware` is opt-in per deployment.

## Alternatives considered

- **Keep sticky-by-session only** — simple but ignores shared prefixes across
  sessions (system prompts, few-shot) and can hot-spot.
- **Precise KV-event index (llm-d/ZMQ)** — highest fidelity but couples us to
  vLLM internals; start with approximate prefix hashing, leave precise as a later
  step.

## Consequences

- Needs a prefix-bucket index (shared across replicas via ADR 0009 state).
- Biggest win on pools serving shared prefixes; negligible for singletons.
- Must preserve capability filtering and the no-cross-model-fallback rule.
