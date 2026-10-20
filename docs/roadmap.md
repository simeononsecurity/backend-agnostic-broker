# Roadmap

Derived from the competitive analysis in
[research/competitive-landscape.md](research/competitive-landscape.md). Each item
links the ADR that records the decision. Every item below is resolved: ✅ done or
❌ rejected (with its ADR link) — nothing is planned or deferred.

## P0 — close the "drop-in OpenAI" gap

| # | Item | Status | ADR |
|---|------|--------|-----|
| 1 | `/v1/completions` + `/v1/responses` (chat-translation shims) | ✅ done | [0006](adr/0006-openai-endpoint-surface.md) |
| 2 | Usage/token + routing-decision log (`/usage`, SQLite) | ✅ done | [0007](adr/0007-usage-accounting-store.md) |
| 2b | `/v1/embeddings` (Ollama native + OpenAI-compat passthrough) | ✅ done — verified live against Ollama (nomic-embed-text, 768-dim) | [0006](adr/0006-openai-endpoint-surface.md) |
| 2c | `/v1/rerank` (Cohere/Jina-compatible; vLLM + llama.cpp passthrough + fake) | ✅ done — verified live against a local llama.cpp `bge-reranker-v2-m3` | [0013](adr/0013-rerank-endpoint.md) |

## P1 — governance, reliability, routing intelligence

| # | Item | Status | ADR |
|---|------|--------|-----|
| 3 | Per-key rate limits (rpm/tpm) + virtual keys + budgets | ✅ done — extended by [0016](adr/0016-governance-v2.md) | [0008](adr/0008-per-key-governance.md) |
| 4 | Retries w/ exponential backoff + member cooldowns | ✅ done | [0009](adr/0009-routing-reliability-and-scale.md), [0017](adr/0017-reliability-v2.md) |
| 5 | Optional Redis shared state (multi-replica) | ✅ done | [0009](adr/0009-routing-reliability-and-scale.md), [0017](adr/0017-reliability-v2.md) |
| 6 | Prefix/cache-aware pool routing + load-aware scoring | ✅ done | [0010](adr/0010-cache-aware-pool-routing.md), extended by [0018](adr/0018-cache-aware-v2.md) |
| 7 | Cascade mode for `auto` (cheap→escalate) | ✅ done | [0011](adr/0011-auto-router-evolution.md) |
| 8 | Latency-budget fallback + privacy/placement pinning | ✅ done | [0011](adr/0011-auto-router-evolution.md) |
| 9 | Router eval harness (CPT/APGR) | ✅ done | [0011](adr/0011-auto-router-evolution.md) |
| 7b | Cascade v2: calibrated reliability scorer + **buffered streaming cascade** | ✅ done | [0019](adr/0019-router-v2.md) |
| 8b | **TTFT** latency estimation for streaming requests | ✅ done | [0019](adr/0019-router-v2.md) |
| 9b | Heuristic code/math accuracy + **frozen held-out** eval split | ✅ done | [0019](adr/0019-router-v2.md) |

**Governance v2 ([ADR 0016](adr/0016-governance-v2.md))** resolved every ADR 0008 / ADR 0011 governance deferral:

- ✅ Selectable **GCRA/token-bucket** rpm (atomic in-process + Redis Lua, per-key burst) and a **sliding-window** tpm counter.
- ✅ **Per-model** rpm/tpm per key (enforced on the resolved model incl. auto + cascade hops) and **hierarchical tenant** token/cost budgets; **per-key placement** policy (unioned, fail-closed).
- ✅ Key **rotation** with a grace overlap and an admin **key-management API** (`/admin/keys`, secret shown once); ❌ retrievable/encrypted keys **rejected** (hash-only is the security property).

## P2 — ops & experience

| # | Item | Status | ADR |
|---|------|--------|-----|
| 10 | Response caching (exact, then optional semantic) | ✅ done — exact cache ([0012](adr/0012-caching-runtime-control-observability.md)); semantic cache + streaming replay + per-request TTL/namespace controls ([0014](adr/0014-semantic-cache-and-cache-extensions.md)) | [0012](adr/0012-caching-runtime-control-observability.md) · [0014](adr/0014-semantic-cache-and-cache-extensions.md) |
| 11 | Runtime control: manual unload + registry hot-reload | ✅ done — unload/load + transactional reload | [0012](adr/0012-caching-runtime-control-observability.md) |
| 12 | OpenTelemetry tracing + optional web UI/playground | ✅ done — tracing (metadata [0012](adr/0012-caching-runtime-control-observability.md); **opt-in span content capture** [0014](adr/0014-semantic-cache-and-cache-extensions.md)); web UI ([0015](adr/0015-web-ui-playground.md)) | [0012](adr/0012-caching-runtime-control-observability.md) · [0014](adr/0014-semantic-cache-and-cache-extensions.md) · [0015](adr/0015-web-ui-playground.md) |

## Notes

- **Reliability v2 (ADR 0017)** resolves every item ADR 0009 deferred under roadmap
  items 4 & 5 — none remains open:
  - ✅ `max_ejection_percent` ejection cap (never-strand) — item 4
  - ✅ Exponential ejection-time growth + decay — item 4
  - ✅ Single-trial half-open probe — item 4
  - ✅ Per-error retry/cooldown policies (4xx never retried; upstream Retry-After) — item 4
  - ✅ Redis sticky global LRU bound — item 5
- Proposed ADRs are **drafts** (direction recorded, not yet built); promote to
  `Accepted` when implementation starts, and add a `## Confirmation` section.
- Invariants that constrain every item: lazy load, **no silent cross-model
  fallback**, non-loading health, security guards (see `AGENTS.md`).
- Item 10 semantic (embedding-similarity) response cache — ✅ done
  ([ADR 0014](adr/0014-semantic-cache-and-cache-extensions.md)).
- Item 12 web UI/playground — ✅ done ([ADR 0015](adr/0015-web-ui-playground.md)).
- **Item 6 cache-aware deferrals — all resolved by [ADR 0018](adr/0018-cache-aware-v2.md):**
  - ✅ queueing model for `scripts/bench_routing.py` (TTFT p50/p95/p99 incl. queueing,
    throughput, utilization) + re-validated default β (`0.25`→`0.5`).
  - ✅ cross-replica prefix-index LRU size bound under Redis (sorted-set LRU).
  - ❌ output-length / queue-aware load signal — Rejected (bench shows no consistent
    gain; in-flight count suffices; real signal via `BROKER_CACHE_AWARE_USE_METRICS`).
  - ❌ token-ID block hashing — Rejected (needs a per-model tokenizer).
  - ❌ precise KV-event (ZMQ) index — Rejected (couples to engine internals).
  - ❌ Preble-style prefix autoscaling — Rejected (scheduler/placement, out of scope).
