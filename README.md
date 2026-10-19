# backend-agnostic-broker

An OpenAI-compatible **local AI broker** that routes requests to multiple
inference backends — **Strata, Ollama, vLLM, llama.cpp** (and future backends via
a small adapter contract). Models load **only when a request selects them**, idle
models **unload automatically**, and GPU placement, tensor/layer split, process
lifetime, and caching are all **configurable**.

> Security-first: child processes never use a shell, model paths are validated
> against allow-listed roots, request-supplied CLI flags are rejected, upstream
> URLs pass an SSRF allow-list, and there is **no silent fallback** to another
> model.

**New here? → [Getting Started guide](docs/getting-started.md)** — install, make
your first request against the built-in fake backend, then connect a real Ollama
backend over the network, all with copy-paste examples.

## Features

- OpenAI-compatible API: `GET /v1/models`, `POST /v1/chat/completions`,
  `POST /v1/completions`, `POST /v1/responses` (all streaming + non-streaming),
  `POST /v1/embeddings`, plus `/healthz`, `/readyz`, `/status`, `/usage`,
  `/metrics` and an optional Ollama-style `/api/tags` discovery endpoint.
- Lazy per-model loading with an explicit lifecycle state machine
  (`unloaded → loading → ready → busy → unloading → failed → stopping`).
- **Load-balancing pools**: route one model name across multiple backend hosts
  with automatic failover — **sticky per-session by default** (pin a conversation
  to one backend), or least-busy / round-robin. See
  [docs/routing-and-health.md](docs/routing-and-health.md).
- **Non-loading health**: `GET /healthz/backends`, a live `health` field in
  `/status`, a background liveness probe that marks dead backends failed, and an
  Ollama `GET /api/ps` passthrough — none of which load a model.
- **`auto` router**: send `"model": "auto"` and a tiny CPU classifier (GBNF-constrained
  llama.cpp GGUF, or a zero-dependency heuristic) picks the best model per request
  from the live registry — with capability filtering, data-driven overrides, and a
  deterministic fallback. The LLM classifier ships self-contained in the **`-llm`**
  image; prioritize models via registry `tags` / `priority` / `cost_tier` /
  `overrides`. See [docs/auto-router.md](docs/auto-router.md).
- Broker-managed process supervision for runtimes that only load a model at
  process startup (vLLM, llama.cpp, Strata); native lazy loading for Ollama.
- Per-model load locking, configurable global/per-model concurrency, idle unload
  sweep, LRU eviction past `max_loaded_models`, request timeouts + cancellation.
- Typed, fail-fast configuration (env vars / mounted registry / Compose).
- Hardened Docker image: multi-stage, non-root, read-only root fs, dropped
  capabilities, CPU fallback for CI, GPU passthrough only via explicit config.
  Published in two variants — **lean** and **`-llm`** (bundled CPU router classifier).

## Quick start (local, fake backend)

```bash
make venv && make install            # create .venv and install deps
cp .env.example .env                 # set BROKER_API_KEYS (or disable auth for dev)
cp registry.example.yaml registry.yaml
# minimal fake-only registry also works; see registry.example.yaml
BROKER_API_KEYS='["my-key"]' BROKER_ENABLED_BACKENDS='["fake"]' make run
```

Then:

```bash
# List models
curl -s -H "Authorization: Bearer my-key" http://127.0.0.1:8080/v1/models

# Chat (non-streaming)
curl -s -H "Authorization: Bearer my-key" -H 'content-type: application/json' \
  -d '{"model":"echo-fake","messages":[{"role":"user","content":"Hello!"}]}' \
  http://127.0.0.1:8080/v1/chat/completions

# Chat (streaming SSE)
curl -N -H "Authorization: Bearer my-key" -H 'content-type: application/json' \
  -d '{"model":"echo-fake","messages":[{"role":"user","content":"Hello!"}],"stream":true}' \
  http://127.0.0.1:8080/v1/chat/completions

# Health / readiness
curl -s http://127.0.0.1:8080/healthz
curl -s http://127.0.0.1:8080/readyz
```

## Quick start (Docker)

Pull a published image — two variants are published per release:

```bash
# lean (the `auto` router uses the zero-dependency heuristic):
docker pull ghcr.io/simeononsecurity/backend-agnostic-broker:latest
# self-contained LLM router (bundled CPU llama.cpp classifier + Q4 GGUF):
docker pull ghcr.io/simeononsecurity/backend-agnostic-broker:latest-llm
```

…or build locally:

```bash
make docker-build                                          # lean image
docker build --build-arg WITH_ROUTER_LLM=1 -t broker:llm . # bundled LLM router
make docker-smoke                                          # build + container smoke test
docker compose up -d --build                               # or the full stack
```

See [docs/docker.md](docs/docker.md) for running/configuring the `-llm` image and
[docs/auto-router.md](docs/auto-router.md) for configuring and prioritizing `auto`.

## Documentation

| Topic | Doc |
|---|---|
| **AI-agent rules** | [AGENTS.md](AGENTS.md) (read first) |
| **Getting started** | [docs/getting-started.md](docs/getting-started.md) |
| Architecture overview | [docs/architecture.md](docs/architecture.md) |
| ADRs | [docs/adr/](docs/adr/) |
| Roadmap | [docs/roadmap.md](docs/roadmap.md) |
| Competitive landscape (research) | [docs/research/competitive-landscape.md](docs/research/competitive-landscape.md) |
| API reference | [docs/api-reference.md](docs/api-reference.md) · [OpenAPI](docs/openapi.yaml) |
| Configuration reference | [docs/configuration.md](docs/configuration.md) |
| Model registry reference | [docs/model-registry.md](docs/model-registry.md) |
| Routing / load balancing / health | [docs/routing-and-health.md](docs/routing-and-health.md) |
| Auto-router (`auto` alias) | [docs/auto-router.md](docs/auto-router.md) |
| Docker guide | [docs/docker.md](docs/docker.md) |
| Compose guide | [docs/docker-compose.md](docs/docker-compose.md) |
| Local dev | [docs/local-dev.md](docs/local-dev.md) |
| Testing | [docs/testing.md](docs/testing.md) |
| CI | [docs/ci.md](docs/ci.md) |
| Secure PR guide | [docs/secure-pr.md](docs/secure-pr.md) |
| Backend adapter guide | [docs/backend-adapter.md](docs/backend-adapter.md) |
| Backends: Strata / Ollama / vLLM / llama.cpp | [docs/backends/](docs/backends/) |
| GPU placement / tensor split / layer split / MTP / cache | [docs/gpu-placement.md](docs/gpu-placement.md), [docs/tensor-split.md](docs/tensor-split.md), [docs/layer-split.md](docs/layer-split.md), [docs/mtp.md](docs/mtp.md), [docs/model-cache.md](docs/model-cache.md) |
| Operations runbook | [docs/operations-runbook.md](docs/operations-runbook.md) |
| Troubleshooting | [docs/troubleshooting.md](docs/troubleshooting.md) |
| Security model / threat model | [docs/security-model.md](docs/security-model.md), [docs/threat-model.md](docs/threat-model.md) |
| Contributing | [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) |
| Changelog | [CHANGELOG.md](CHANGELOG.md) |
| Acceptance-gate evidence | [docs/EVIDENCE.md](docs/EVIDENCE.md) |

## License

Apache-2.0 (placeholder; see image labels).
