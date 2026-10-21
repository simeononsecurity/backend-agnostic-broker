# Documentation

Everything for running, configuring, and extending the backend-agnostic-broker.
Organized by [Diátaxis](https://diataxis.fr/): a learning **tutorial**,
task-oriented **how-to guides**, exhaustive **reference**, and **explanation**
(ADRs).

## Start here

- **[Getting Started tutorial](getting-started.md)** — zero → first chat in three
  tracks (fake backend / local Ollama / Docker).
- **[How-to guides](guides/README.md)** — one per feature, task-oriented.
- **[Examples](../examples/README.md)** — runnable, tested example dirs.

## How-to guides

[backends](guides/backends.md) · [pools & routing](guides/pools-and-routing.md) ·
[reliability](guides/reliability.md) · [redis multi-replica](guides/redis-multi-replica.md) ·
[auto-router](guides/auto-router.md) · [virtual keys](guides/virtual-keys.md) ·
[response cache](guides/response-cache.md) · [admin API](guides/admin-api.md) ·
[web UI](guides/web-ui.md) · [observability](guides/observability.md) ·
[embeddings & rerank](guides/embeddings-and-rerank.md) ·
[restart & recovery](guides/restart-and-recovery.md) ·
[production deployment](guides/production-deployment.md) ·
[upgrading](guides/upgrading.md)

## Reference

| Topic | Doc |
|---|---|
| API | [api-reference.md](api-reference.md) · [openapi.yaml](openapi.yaml) |
| Configuration (every `BROKER_*` setting) | [configuration.md](configuration.md) |
| Model registry schema | [model-registry.md](model-registry.md) |
| Routing / load balancing / health | [routing-and-health.md](routing-and-health.md) |
| Auto-router | [auto-router.md](auto-router.md) |
| Backend adapter contract | [backend-adapter.md](backend-adapter.md) |
| Backends | [Ollama](backends/ollama.md) · [vLLM](backends/vllm.md) · [llama.cpp](backends/llamacpp.md) · [Strata](backends/strata.md) |
| GPU placement / tensor split / layer split / MTP / cache | [gpu-placement.md](gpu-placement.md) · [tensor-split.md](tensor-split.md) · [layer-split.md](layer-split.md) · [mtp.md](mtp.md) · [model-cache.md](model-cache.md) |
| Web UI | [web-ui.md](web-ui.md) |
| Docker / Compose | [docker.md](docker.md) · [docker-compose.md](docker-compose.md) |
| Testing / CI / secure PR | [testing.md](testing.md) · [ci.md](ci.md) · [secure-pr.md](secure-pr.md) |

## Explanation & operations

| Topic | Doc |
|---|---|
| Architecture overview | [architecture.md](architecture.md) |
| Architecture Decision Records | [adr/](adr/) |
| Roadmap | [roadmap.md](roadmap.md) |
| Security model / threat model | [security-model.md](security-model.md) · [threat-model.md](threat-model.md) |
| Operations runbook | [operations-runbook.md](operations-runbook.md) |
| Troubleshooting | [troubleshooting.md](troubleshooting.md) |
| Local dev | [local-dev.md](local-dev.md) |
| Competitive landscape (research) | [research/](research/) |
| Acceptance-gate evidence | [EVIDENCE.md](EVIDENCE.md) |
| Contributing | [CONTRIBUTING.md](CONTRIBUTING.md) |

## For AI agents

Read [AGENTS.md](../AGENTS.md) first — it is the binding change-contract for
automated contributors.
