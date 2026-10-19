# Roadmap

Derived from the competitive analysis in
[research/competitive-landscape.md](research/competitive-landscape.md). Each item
links the ADR that records the decision. Status: ✅ done · 🔜 next · 📋 planned
(proposed ADR draft).

## P0 — close the "drop-in OpenAI" gap

| # | Item | Status | ADR |
|---|------|--------|-----|
| 1 | `/v1/completions` + `/v1/responses` (chat-translation shims) | ✅ done | [0006](adr/0006-openai-endpoint-surface.md) |
| 2 | Usage/token + routing-decision log (`/usage`, SQLite) | ✅ done | [0007](adr/0007-usage-accounting-store.md) |
| 2b | `/v1/embeddings` (Ollama native + OpenAI-compat passthrough) | ✅ done — verified live against Ollama (nomic-embed-text, 768-dim) | [0006](adr/0006-openai-endpoint-surface.md) |
| 2c | `/v1/rerank` | 📋 planned | [0006](adr/0006-openai-endpoint-surface.md) |

## P1 — governance, reliability, routing intelligence

| # | Item | Status | ADR |
|---|------|--------|-----|
| 3 | Per-key rate limits (rpm/tpm) + virtual keys + budgets | ✅ done | [0008](adr/0008-per-key-governance.md) |
| 4 | Retries w/ exponential backoff + member cooldowns | ✅ done | [0009](adr/0009-routing-reliability-and-scale.md) |
| 5 | Optional Redis shared state (multi-replica) | ✅ done | [0009](adr/0009-routing-reliability-and-scale.md) |
| 6 | Prefix/cache-aware pool routing + load-aware scoring | ✅ done | [0010](adr/0010-cache-aware-pool-routing.md) |
| 7 | Cascade mode for `auto` (cheap→escalate) | ✅ done | [0011](adr/0011-auto-router-evolution.md) |
| 8 | Latency-budget fallback + privacy/placement pinning | ✅ done | [0011](adr/0011-auto-router-evolution.md) |
| 9 | Router eval harness (CPT/APGR) | ✅ done | [0011](adr/0011-auto-router-evolution.md) |

## P2 — ops & experience

| # | Item | Status | ADR |
|---|------|--------|-----|
| 10 | Response caching (exact, then optional semantic) | ✅ done — exact cache ([0012](adr/0012-caching-runtime-control-observability.md)); semantic cache + streaming replay + per-request TTL/namespace controls ([0014](adr/0014-semantic-cache-and-cache-extensions.md)) | [0012](adr/0012-caching-runtime-control-observability.md) · [0014](adr/0014-semantic-cache-and-cache-extensions.md) |
| 11 | Runtime control: manual unload + registry hot-reload | ✅ done — unload/load + transactional reload | [0012](adr/0012-caching-runtime-control-observability.md) |
| 12 | OpenTelemetry tracing + optional web UI/playground | ✅ tracing (metadata [0012](adr/0012-caching-runtime-control-observability.md); **opt-in span content capture** [0014](adr/0014-semantic-cache-and-cache-extensions.md)); ❌ web UI still deferred ([0012](adr/0012-caching-runtime-control-observability.md), `BROKER_UI_ENABLED` reserved) | [0012](adr/0012-caching-runtime-control-observability.md) · [0014](adr/0014-semantic-cache-and-cache-extensions.md) |

## Notes

- Proposed ADRs are **drafts** (direction recorded, not yet built); promote to
  `Accepted` when implementation starts, and add a `## Confirmation` section.
- Invariants that constrain every item: lazy load, **no silent cross-model
  fallback**, non-loading health, security guards (see `AGENTS.md`).
- Deferred follow-ups: web UI/playground (item 12, `BROKER_UI_ENABLED` reserved);
  and a **queueing model for `scripts/bench_routing.py`** so the cache-aware
  TTFT/p95 figures reflect load-imbalance queueing rather than routing-only
  savings (ADR 0010). The semantic (embedding-similarity) response cache (item 10)
  is now **done** (ADR 0014).
