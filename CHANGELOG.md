# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-10-16

Second local release (not published; commits and tag are local-only). Adds
deployment-config portability (registry env-var substitution + local `.env`
loader) and verified live remote-backend routing, with a streaming fix.

### Added

- **Registry env-var substitution**: registry files now expand `${VAR}` /
  `${VAR:-default}` references from the environment at load time
  (`broker/registry.py::_expand_env`), applied to parsed **string values only**
  (never comments or keys), so deployment-specific endpoints (e.g. a remote
  Ollama daemon's URL) stay out of version control — set them in a gitignored
  `.env` and reference `base_url: ${OLLAMA_BASE_URL}`. A referenced variable that
  is unset with no default fails fast.
- **Local `.env` loader** in `python -m broker`
  (`broker/__main__.py::_load_local_env`, stdlib-only, non-overriding) so
  non-`BROKER_`-prefixed vars referenced by the registry are available; Docker/
  compose/secret injection still takes precedence.
- **Live remote-ollama-host integration evidence**: verified the local broker routing to a
  remote Ollama backend over tailscale (models list, non-streaming + streaming
  completions, lazy-load, idle-unload). See `docs/EVIDENCE.md` → "Follow-up:
  live integration against the `remote-ollama-host` Ollama backend".

### Fixed

- **Streaming**: `HTTPBackendAdapter.stream_chat` now drops the upstream
  `data: [DONE]` sentinel and emits exactly one terminal marker, preventing a
  duplicate `[DONE]` when proxying backends (e.g. Ollama) that send their own.

## [0.1.0] - 2026-10-16

Initial local release (not published; commits are local-only).

### Added

- **Broker core**: OpenAI-compatible FastAPI service with lazy per-model
  loading, an explicit lifecycle state machine
  (`unloaded → loading → ready → busy → unloading → failed → stopping`),
  per-model load locking, global + per-model concurrency limits, idle-unload
  sweep, and LRU eviction past `max_loaded_models`.
- **Adapters**: uniform backend contract (`base.py`), shared HTTP adapter
  (`httpbase.py`), and adapters for `fake`, `ollama`, `vllm`, `llamacpp`, and
  `strata`. Ollama uses native `keep_alive`; vLLM/llama.cpp/Strata are
  broker-managed child processes.
- **API**: `GET /v1/models`, `POST /v1/chat/completions` (streaming +
  non-streaming), `GET /healthz`, `GET /readyz`, `GET /status`, `GET /metrics`,
  and an optional Ollama-style `GET /api/tags`. API-key auth (Bearer /
  `X-API-Key`), request IDs, structured errors, request-size and concurrency
  limits, timeouts, and cancellation.
- **Configuration**: typed, fail-fast `Settings` (env prefix `BROKER_`),
  declarative pydantic model registry (YAML/JSON), `.env.example`,
  `registry.example.yaml`.
- **Security**: executable allow-listing, model-path validation (traversal/
  symlink-safe), request-flag rejection, no-shell argv construction, and an SSRF
  guard; no silent fallback.
- **Docker**: hardened multi-stage image (non-root, read-only rootfs, dropped
  capabilities, healthcheck), base + GPU Compose files, Makefile, smoke test.
- **CI**: fork-safe PR workflow (lint/format/type/tests, Compose validation,
  secret scan, image build + smoke, SBOM, Trivy scan) and a gated release
  workflow (no default image publishing).
- **Docs**: README, architecture overview, ADRs (language choice, architecture
  review), API reference, OpenAPI spec, configuration + registry references,
  Docker/Compose/local-dev/testing/CI/secure-PR guides, backend lazy-loading
  guides, GPU/tensor-split/layer-split/MTP/cache guides, operations runbook,
  troubleshooting, security model, threat model, contributing guide, and an
  acceptance-gate evidence ledger.
- **Tests**: 67 unit/integration/API tests (config, registry, lifecycle,
  adapters, API, auth, streaming, lazy-load, unload, security, docs-examples).

[Unreleased]: https://example.local/backend-agnostic-broker
[0.2.0]: https://example.local/backend-agnostic-broker
[0.1.0]: https://example.local/backend-agnostic-broker
