# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-10-18

### Added

- **`POST /v1/completions`** (legacy) and **`POST /v1/responses`** (OpenAI
  Responses API), implemented as translation shims over the chat pipeline — they
  inherit routing, pools, lazy-load, failover, and the `auto` router; streaming +
  non-streaming. Unknown models still 404 (no silent fallback). See
  [ADR 0006](docs/adr/0006-openai-endpoint-surface.md).
- **`POST /v1/embeddings`** via a new `embeddings()` adapter method — Ollama native
  `/api/embed`, OpenAI-compatible passthrough for vLLM/llama.cpp, deterministic
  fake for CI. Routes by model name through pool/lazy-load/failover; chat-only
  models return 400. Verified end-to-end against a live Ollama backend
  (nomic-embed-text, 768-dim). See [ADR 0006](docs/adr/0006-openai-endpoint-surface.md).
- **Request usage/decision log** (SQLite) + **`GET /usage`**: per-request token
  counts, latency, requested-vs-served model, endpoint, and a non-reversible API-key
  fingerprint (metadata only, never content). Config: `BROKER_USAGE_LOG_ENABLED`,
  `BROKER_USAGE_LOG_PATH`. See [ADR 0007](docs/adr/0007-usage-accounting-store.md).

### Docs

- Competitive-landscape research ([docs/research/competitive-landscape.md](docs/research/competitive-landscape.md))
  and a [roadmap](docs/roadmap.md) with proposed ADR drafts 0008–0012 (per-key
  governance; routing reliability + optional Redis shared state; cache-aware pool
  routing; auto-router evolution — cascade/latency-budget/privacy-pinning/eval;
  caching + runtime control + OpenTelemetry).

## [0.3.1] - 2026-10-18

### Documentation

- Document the bundled-LLM router across the user-facing docs: README + Getting
  Started now cover the two published image variants (lean vs `-llm`) and GHCR
  pulls; `docs/docker.md` adds pulling, running, and swapping the bundled
  classifier; `docs/auto-router.md` adds running the LLM outside the container and
  verifying `via: llm` vs a heuristic fallback.
- Annotate the auto-router configuration surface in the shipped examples:
  `registry.example.yaml` documents every per-model router field and the full
  `router` block (policy, weights, overrides); `.env.example` describes every
  `BROKER_ROUTER_*` setting.

## [0.3.0] - 2026-10-18

### Added

- **`auto` router alias**: a tiny classifier that, given a request plus the live
  candidate list, routes to the single best model id. Only the `auto` alias is
  affected. Pipeline: derive flags (images/tools/json/ctx) → Stage-1 capability
  filter → data-driven hard overrides → classifier → validate → deterministic
  fallback → LRU decision cache.
  - Two backends: a zero-dependency **heuristic** scorer (default/fallback) and a
    CPU **llama.cpp GGUF** classifier constrained by a GBNF grammar to the exact
    candidate ids (`BROKER_ROUTER_BACKEND=auto|llm|heuristic|off`).
  - Model-agnostic: candidates are injected at inference time from the registry,
    so adding/removing a model changes routing with no retrain.
  - Customization is all data: per-model `tags`/`description`/`priority`/
    `cost_tier`/`router_eligible`, plus a registry `router` block
    (`default`, `policy`, `overrides`, `weights`).
  - `auto` is listed in `GET /v1/models`; `GET /status.router` shows backend,
    cache size, and recent decisions. Dockerfile `--build-arg WITH_ROUTER_LLM=1`
    bundles the CPU wheel + a small Q4 GGUF for a self-contained classifier.
  - Docs: [docs/auto-router.md](docs/auto-router.md); `BROKER_ROUTER_*` settings.

### Changed

- **Dockerfile refactored to 3 stages** (`pydeps` → `router` → `runtime`): the
  compile toolchain and build wheels now live only in build stages and the final
  image receives just the installed packages (relocatable prefix), so there is no
  wheels layer or compiler in the published image. Lean image ≈ 272 MB.
- **Release workflow publishes two image variants** on each tag: lean
  (`:X.Y.Z`, `:latest`) and the bundled-LLM router (`:X.Y.Z-llm`, `:latest-llm`,
  built with `WITH_ROUTER_LLM=1`). The LLM variant installs `libgomp1` for the
  compiled llama.cpp runtime and bundles a small Q4 GGUF.

## [0.2.0] - 2026-10-17

### Added

- **Sticky session routing** (now the **default** `route_strategy`): each session
  is pinned 1:1 to a pool member and reused across requests (warm cache / KV
  locality), switching members only on failure/unavailability (failover re-pins).
  Session key = `X-Session-Id` header, else the OpenAI `user` field, else client
  IP; new sessions are assigned the least-busy member so distinct sessions still
  spread. Bounded LRU map via `BROKER_ROUTE_STICKY_MAX_ENTRIES` (default 10000).
  `GET /status` now reports `route_strategy` and `sticky_sessions`.

### Changed

- `BROKER_ROUTE_STRATEGY` default changed from `least_busy` to `sticky`;
  `least_busy` and `round_robin` remain available. (A request with no derivable
  session key behaves like `least_busy`.)

### Added (load balancing & health — earlier this release)

- **Load-balancing pools**: registry entries sharing a `pool` name form one
  logical model; requests are routed across members per `BROKER_ROUTE_STRATEGY`
  with the in-flight slot reserved at selection so concurrent callers spread
  across hosts instead of piling onto one.
- **Automatic failover**: a request whose chosen pool member is unavailable
  transparently retries another member (before first byte for streams); failed
  members are marked unhealthy and excluded until they recover. No silent
  fallback to a *different* model.
- **Non-loading health surfaces**: `GET /healthz/backends` (live per-member
  liveness — Ollama via `/api/tags`, managed runtimes only while running,
  unloaded = `not_loaded`), a `healthy`/`last_health` field and `pools` map in
  `GET /status`, and a background liveness probe
  (`BROKER_HEALTH_PROBE_ENABLED` / `_INTERVAL_SECONDS` / `_TIMEOUT_SECONDS`) that
  marks a `ready` model `failed` when its backend has gone away so the next
  request reloads/reroutes.
- **`GET /api/ps`**: passthrough of Ollama `/api/ps` across daemons (deduped by
  base URL) showing models actually resident in memory — distinct from the
  broker's lifecycle state. Non-loading.
- Docs: [docs/routing-and-health.md](docs/routing-and-health.md); pool example in
  `registry.example.yaml`; new routing/health env vars in `.env.example`.

## [0.1.0] - 2026-10-17

Initial release.

### Added

- **Broker core**: OpenAI-compatible FastAPI service with lazy per-model loading,
  an explicit lifecycle state machine
  (`unloaded → loading → ready → busy → unloading → failed → stopping`),
  per-model load locking, global + per-model concurrency limits, idle-unload
  sweep, and LRU eviction past `max_loaded_models`.
- **Adapters**: uniform backend contract (`base.py`), shared HTTP adapter
  (`httpbase.py`), and adapters for `fake`, `ollama`, `vllm`, `llamacpp`, and
  `strata`. Ollama uses native `keep_alive`; vLLM/llama.cpp/Strata are
  broker-managed child processes. Streaming proxies emit exactly one terminal
  `data: [DONE]` (upstream duplicates are dropped).
- **API**: `GET /v1/models`, `POST /v1/chat/completions` (streaming +
  non-streaming), `GET /healthz`, `GET /readyz`, `GET /status`, `GET /metrics`,
  and an optional Ollama-style `GET /api/tags`. API-key auth (Bearer /
  `X-API-Key`), request IDs, structured errors, request-size and concurrency
  limits, timeouts, and cancellation.
- **Configuration**: typed, fail-fast `Settings` (env prefix `BROKER_`),
  declarative pydantic model registry (YAML/JSON), `.env.example`,
  `registry.example.yaml`. Registry files expand `${VAR}` / `${VAR:-default}`
  references from the environment at load time (parsed string values only), so
  deployment-specific endpoints (e.g. a remote Ollama daemon's URL) stay out of
  version control; `python -m broker` loads a local `.env` (stdlib-only,
  non-overriding, overridable by real env / Docker secrets).
- **Security**: executable allow-listing, model-path validation
  (traversal/symlink-safe), request-flag rejection, no-shell argv construction,
  and an SSRF guard (each registry `base_url` is auto-trusted); no silent
  fallback.
- **Docker**: hardened multi-stage image (non-root, read-only rootfs, dropped
  capabilities, healthcheck), base + GPU Compose files, Makefile, smoke test.
- **CI**: fork-safe PR workflow (lint/format/type/tests, Compose validation,
  secret scan, image build + smoke, SBOM, Trivy scan) and a release workflow that
  builds, smoke-tests, Trivy-scans, and then publishes the image to GHCR
  (`:<version>` + `:latest`) on semver tag push via the repo-scoped
  `GITHUB_TOKEN`.
- **Docs**: README, Getting Started guide, architecture overview, ADRs, API
  reference + OpenAPI spec, configuration + registry references,
  Docker/Compose/local-dev/testing/CI/secure-PR guides, backend lazy-loading
  guides, GPU/tensor-split/layer-split/MTP/cache guides, operations runbook,
  troubleshooting, security model, threat model, contributing guide, and an
  acceptance-gate evidence ledger (including a verified live remote-Ollama
  integration: models list, non-streaming + streaming completions, lazy-load,
  idle-unload).
- **Tests**: 71 unit/integration/API tests (config, registry incl. env-var
  substitution, lifecycle, adapters, API, auth, streaming, lazy-load, unload,
  security, docs-examples).

[0.1.0]: https://github.com/simeononsecurity/backend-agnostic-broker/releases/tag/v0.1.0
