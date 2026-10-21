# How-to guides

Task-oriented guides — each answers "how do I do X" with a when-to-use note,
minimal and full config, a verify step with expected output, troubleshooting, a
runnable example, and links to the reference page and the ADR behind it.

New to the broker? Start with the [Getting Started tutorial](../getting-started.md),
then come back here for the feature you need.

## Guides

| Guide | Covers | Example |
|---|---|---|
| [backends](backends.md) | Ollama, vLLM, llama.cpp, Strata, external URL | [ollama-quickstart](../../examples/ollama-quickstart/) |
| [pools-and-routing](pools-and-routing.md) | Pools + `sticky` / `least_busy` / `round_robin` / `cache_aware` | [cache-aware-routing](../../examples/cache-aware-routing/) |
| [reliability](reliability.md) | Retries, cooldowns, ejection cap, half-open probes | [reliability](../../examples/reliability/) |
| [redis-multi-replica](redis-multi-replica.md) | Shared state across broker replicas | [docker-compose-redis](../../examples/docker-compose-redis/) |
| [auto-router](auto-router.md) | `auto`: heuristic/LLM, cascade, latency budget, placement | [auto-router](../../examples/auto-router/) |
| [virtual-keys](virtual-keys.md) | Keys, limits, budgets, tenants, rotation | [governance](../../examples/governance/) |
| [response-cache](response-cache.md) | Exact + semantic cache, streaming replay | [caching](../../examples/caching/) |
| [admin-api](admin-api.md) | Admin key API + registry hot reload | [governance](../../examples/governance/) |
| [web-ui](web-ui.md) | The optional `/ui` playground | [web-ui](../../examples/web-ui/) |
| [observability](observability.md) | OTel tracing, usage log, `/status`, `/metrics` | [otel](../../examples/otel/) |
| [embeddings-and-rerank](embeddings-and-rerank.md) | `/v1/embeddings`, `/v1/rerank`, RAG | [rag-pipeline](../../examples/rag-pipeline/) |
| [gpu-memory](gpu-memory.md) | GPU/host telemetry, measured release, sized models | [gpu-memory](../../examples/gpu-memory/) |
| [production-deployment](production-deployment.md) | Docker, reverse proxy/TLS, non-root, security checklist | [docker-compose.yml](../../docker-compose.yml) |
| [upgrading](upgrading.md) | Upgrading from 0.4 / 0.5 (what's opt-in) | — |

## See also

- [Getting Started tutorial](../getting-started.md) — zero → first chat in three tracks.
- [Documentation index](../README.md) — reference, architecture, ADRs.
- [Examples](../../examples/README.md) — every example, tested.
