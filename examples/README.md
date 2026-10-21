# Examples

Runnable, **tested** examples — one per broker feature. Every example here is
exercised by the test suite (`tests/test_examples.py` plus the per-feature test
modules), so it cannot silently drift from the code. Each uses the dependency-free
`fake` backend where possible, so you can run it with **no GPU, no downloads, and
no external daemon**, then swap in real backends for production.

> **Placeholders only.** These files never contain real hosts, IPs, or keys. Copy
> an example's `.env.example` / `registry.example.yaml` to a gitignored `.env` /
> `registry.yaml` and fill in your own values (`<OLLAMA_HOST>`, `${OLLAMA_BASE_URL}`,
> `YOUR_API_KEY`).

## Index

| Example | What it shows | Guide |
|---|---|---|
| [ollama-quickstart](ollama-quickstart/) | Connect a real Ollama backend (offline fake fallback) | [backends](../docs/guides/backends.md) |
| [cache-aware-routing](cache-aware-routing/) | Pools + `cache_aware` routing over a 3-member pool | [pools-and-routing](../docs/guides/pools-and-routing.md) |
| [reliability](reliability/) | Retries, cooldowns, ejection cap, half-open probes | [reliability](../docs/guides/reliability.md) |
| [docker-compose-redis](docker-compose-redis/) | Two broker replicas sharing state via Redis | [redis-multi-replica](../docs/guides/redis-multi-replica.md) |
| [auto-router](auto-router/) | `auto` model: heuristic, cascade, latency budget, placement | [auto-router](../docs/guides/auto-router.md) |
| [governance](governance/) | Virtual keys, limits, budgets, tenants, rotation, admin API | [virtual-keys](../docs/guides/virtual-keys.md) |
| [caching](caching/) | Exact + semantic response cache, streaming replay | [response-cache](../docs/guides/response-cache.md) |
| [web-ui](web-ui/) | The optional `/ui` playground | [web-ui](../docs/guides/web-ui.md) |
| [otel](otel/) | OpenTelemetry tracing → Collector → Jaeger | [observability](../docs/guides/observability.md) |
| [rerank](rerank/) | The `/v1/rerank` endpoint | [embeddings-and-rerank](../docs/guides/embeddings-and-rerank.md) |
| [rag-pipeline](rag-pipeline/) | embed → retrieve → rerank → chat | [embeddings-and-rerank](../docs/guides/embeddings-and-rerank.md) |
| [restart-recovery](restart-recovery/) | Runtime ledger + restart reconciliation (adopt/stop/report) | [restart-and-recovery](../docs/guides/restart-and-recovery.md) |
| [gpu-memory](gpu-memory/) | Telemetry, measured release, sized models (ADR 0020/0022) | [gpu-memory](../docs/guides/gpu-memory.md) |
| [request-queueing](request-queueing/) | Bounded, fair admission queue under overload (ADR 0023) | [request-queueing](../docs/guides/request-queueing.md) |

## How these are tested

- `tests/test_examples.py` — generic coverage: every example dir has a README,
  every `registry*.yaml` loads through the real loader, every `keys*.yaml` parses,
  every `BROKER_*` env key is a real `Settings` field, and every `docker-compose*.yml`
  parses and references only expected images. It also runs the new examples'
  documented requests against the fake backend.
- Per-feature modules (`tests/test_example_*.py`, `tests/test_examples_*.py`,
  `tests/test_ui.py`, `tests/test_docs_examples.py`) drive each example's specific
  behavior end-to-end.

## New here?

Start with the [Getting Started tutorial](../docs/getting-started.md), then pick a
[how-to guide](../docs/guides/README.md) and its example above.
