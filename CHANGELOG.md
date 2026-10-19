# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Governance v2** (ADR 0016) — resolves every deferred per-key governance item:
  - **Selectable rate-limit algorithms.** `BROKER_RATE_LIMIT_ALGO` =
    `sliding_log` (exact, default) or `gcra` (atomic GCRA/token-bucket with
    per-key burst via `limits.rpm_burst`), backed by a new atomic
    `StateStore.gcra` primitive (in-process + Redis Lua, single round-trip).
    `BROKER_RATE_LIMIT_TPM_ALGO` = `fixed` (default) or `sliding` (weighted
    two-bucket sliding-window counter).
  - **Per-model limits** — `limits.per_model: {model: {rpm, tpm}}` enforced on
    the resolved model incl. the `auto` pick and every cascade hop.
  - **Hierarchical tenant budgets** — a `tenants:` block in `keys.yaml` caps
    aggregate daily/monthly token + cost across all keys of a tenant.
  - **Per-key placement** — `limits.placement` unioned (fail-closed) into the
    request placement requirement (`403 placement_denied`).
  - **Key rotation** — `python -m broker.keys rotate <id> --grace-days N`
    (integer `0..365`) mints a successor with a grace overlap (`0` = revoke now).
    Rotation sets the old expiry to `min(existing, today+N)` so it can only
    **shorten** a key's life, and is **refused on a revoked/expired key**
    (`409 key_not_active`) so a dead key is never resurrected (CLI parity).
  - **Admin key-management API** — `GET/POST /admin/keys`,
    `POST /admin/keys/{id}/rotate`, `DELETE /admin/keys/{id}` (admin-scope only,
    secret shown once, never logged). Create/rotate inputs are validated
    (`grace_days`, ISO non-past `expiry`, charset/length-capped `tenant`/`label`)
    → `400` rather than a stored dead key or a `500`; oversized bodies → `413`.
    `keys.yaml` writes are atomic (temp file + `fsync` + `os.replace`) under an
    exclusive `flock` on a `keys.yaml.lock` sidecar, created `0600` (stricter
    existing mode preserved); a reload over a corrupt file keeps the last good key
    set and logs an error.
  - Governance now **fails closed** on a `StateStore` error (request denied).
  - **Rejected:** retrievable/encrypted keys — hash-only at rest is the security
    property; rotation covers the operational need.
- CLI `broker.keys create` gains `--rpm-burst`, `--placement`, `--per-model`;
  new `rotate` subcommand.

### Notes

- **Defaults reproduce v0.5.0 byte-for-byte** (`sliding_log` rpm, `fixed` tpm, no
  per-model/tenant limits, no per-key placement; governance inert with no keys
  file). The `[dev]` extra now pulls `fakeredis[lua]` to exercise the Redis Lua
  path in tests.

## [0.5.0] - 2026-10-19

### Added

- **Retries with full-jitter backoff** (ADR 0009): `BROKER_RETRY_MAX_ATTEMPTS`,
  `BROKER_RETRY_BACKOFF_BASE_MS`, `BROKER_RETRY_BACKOFF_MAX_MS`. Retries re-select
  a member **within the requested model's pool only** (never a different model),
  wait `uniform(0, min(max, base·2^n))` between attempts, respect the request
  timeout as a deadline, and never retry once a stream is emitting. **Defaults
  (`0`/`0`) reproduce v0.4.0 exactly** — one failover pass, no sleep.
- **Member cooldowns / circuit breaker** (ADR 0009): `BROKER_COOLDOWN_FAILURES`,
  `BROKER_COOLDOWN_SECONDS`. After N consecutive failures a pool member is benched
  and skipped; after the window it half-opens for a trial (success resets, failure
  re-benches). Visible per member in `GET /status` and `GET /healthz/backends`.
  Default-off preserves current behavior.
- **`StateStore` abstraction** (`broker/state.py`, ADR 0009): one async API over
  the sticky map, counters (+ sliding-window helper), cooldown records, and a
  generic bytes/JSON KV. `InProcessStateStore` is the default; **optional
  `RedisStateStore`** (`BROKER_REDIS_URL`, `BROKER_REDIS_PREFIX`,
  `BROKER_ROUTE_STICKY_TTL_SECONDS`) shares state across replicas. `redis` is an
  optional extra (`pip install 'backend-agnostic-broker[redis]'`), import-guarded
  → `ConfigError` if enabled-but-missing. One shared `StateStore` instance backs
  the sticky map, the ADR 0008 governance counters, the ADR 0010 prefix index, and
  the ADR 0012 response cache.
- **Cache-aware pool routing** (ADR 0010): opt-in `BROKER_ROUTE_STRATEGY=cache_aware`
  routes each request to the pool member that already holds its prompt **prefix**
  (warm KV cache), scoring `score = matched_fraction - beta·relative_load` and
  falling back to `least_busy` when no member holds any prefix. Prompts are
  normalized and sliced into fixed-size **chained** character blocks
  (`BROKER_CACHE_AWARE_BLOCK_CHARS`); the prefix→member index lives on the
  `StateStore` (shared via Redis when configured), LRU-bounded
  (`BROKER_CACHE_AWARE_MAX_ENTRIES`) with a TTL (`BROKER_CACHE_AWARE_TTL_SECONDS`).
  `BROKER_CACHE_AWARE_BETA` is the single cache-vs-load knob (**default `0.25`**,
  tuned from the benchmark — `1.0` over-penalized affinity);
  `BROKER_CACHE_AWARE_USE_METRICS` lets the **non-loading** health probe scrape
  vLLM `/metrics` (`vllm:num_requests_waiting`) for a real load signal (falls back
  to in-flight counts). Default (`sticky`) behavior is unchanged.
  `scripts/bench_routing.py` ships four seeded scenarios (long-lived, cache
  pressure, cross-session sharing, skewed/bursty) with a `--matrix` scenario×β mode
  and a `--capacity-sweep`, and runs without `PYTHONPATH`; `docs/routing-and-health.md`
  records the matrix, the default-β selection rule, and a "when to use cache_aware
  vs sticky" guide (cache_aware wins under prefix reuse + cache pressure, `sticky`
  wins under bursty skewed load).
- **Auto-router evolution (ADR 0011)** — four opt-in, default-off routing features;
  with none configured, `auto` behaves exactly as v0.4.0:
  - **Cascade mode** (`router.mode: cascade`): serve the cheapest capable model
    first, inspect the actual answer, and escalate **once** to a stronger capable
    model on a data-driven signal (`finish_reason=length`, empty/too-short output,
    or a refusal regex — all in `router.cascade`). Both hops are recorded (usage +
    decision log) and the response header `x-broker-route` shows the path
    (`tiny->coder`). Streaming falls back to the single strong pick (documented).
  - **Latency budget**: `x-broker-latency-budget-ms` header (+ registry
    `router.latency_budget_ms` / `BROKER_ROUTER_LATENCY_BUDGET_MS`) picks a faster
    capable model when the pick's estimated latency (usage-log p90 + cold-start
    penalty) exceeds the budget; never overrides capability/placement.
  - **Placement / data-residency pinning (fail-closed)**: per-model `placement`
    labels + `x-broker-placement` header and `BROKER_DEFAULT_PLACEMENT`; candidates,
    explicit models, and pool members are filtered to those carrying all required
    labels, else `403 placement_denied` — never spills to a non-compliant model.
  - **Eval harness**: `scripts/router_eval.py` + `tests/data/router_eval.jsonl`
    (45 labeled prompts) computes accuracy, cost-weighting, and RouteLLM
    PGR/APGR/CPT — PGR against a true quality ceiling (perfect-oracle accuracy,
    not the most-expensive model), APGR/CPT swept from the router's own
    cost-penalty curve (≥10 points) with the oracle label frontier reported
    separately as an upper bound; PGR is raw (no clamping) and degenerate/out-of-
    range values are flagged `invalid` with a reason. Markdown/JSON report; runs
    offline on the heuristic backend.
  - New settings: `BROKER_ROUTER_LATENCY_BUDGET_MS`, `BROKER_ROUTER_LATENCY_WINDOW_SECONDS`,
    `BROKER_ROUTER_COLD_START_MS`, `BROKER_DEFAULT_PLACEMENT`. New error type
    `placement_denied` (403); usage log gains `latency_percentiles`.
    See [docs/auto-router.md](docs/auto-router.md),
    [ADR 0011](docs/adr/0011-auto-router-evolution.md), and the research note.
- **Per-key governance: virtual keys, rate limits & budgets** (ADR 0008). Opt-in
  via `BROKER_KEYS_PATH` (a git-ignored `keys.yaml` of **hashed** virtual keys;
  manage with `python -m broker.keys create|list|revoke`, secret shown once).
  Each key has an id/label/tenant/scope (`user`|`admin`), optional expiry, and
  limits: `rpm` (sliding window), `tpm` (windowed, pre-dispatch estimate +
  post-response reconcile with real usage), `max_concurrency`, daily/monthly
  **token budgets**, optional **cost budgets** (registry `pricing` block, USD per
  million tokens), and a fail-closed `allowed_models` list (applies to the model
  the `auto` router picks and to each cascade hop). Counters live on the ADR 0009
  `StateStore` (in-process or Redis). Enforced before dispatch on
  chat/completions/responses/embeddings with OpenAI-compatible errors: `429`
  `rate_limit_exceeded` + `Retry-After` + `x-ratelimit-limit/remaining/reset-{requests,tokens}`
  headers (also emitted on success), `429` `insufficient_quota` for budgets, and
  `403` `model_not_allowed`. `GET /usage` is now **scoped** — a `user` key sees
  only its own usage; an `admin` key sees all keys plus a per-key spend breakdown.
  Legacy `BROKER_API_KEYS` keep working as **unlimited user keys**; with no keys
  file and no limits, behavior is identical to v0.4.0. New settings
  `BROKER_KEYS_PATH`, `BROKER_RATE_LIMIT_WINDOW_SECONDS`; new registry `pricing`
  block; `keys.yaml` git-ignored (`keys.example.yaml` tracked).
- **Exact response cache** (ADR 0012, default off): `BROKER_RESPONSE_CACHE_ENABLED`
  with TTL / `MAX_ENTRIES` / `MAX_BYTES` / `DETERMINISTIC_ONLY`. Identical requests
  to the same model are served from a StateStore-backed cache (in-process or
  Redis), skipping inference. Key = hash of (API-key/tenant scope, public model
  **after `auto` resolution**, normalized messages/prompt/input + sampling +
  tools + `response_format`), so a cached answer **never crosses a key/tenant
  boundary**. Deterministic requests only by default (`temperature == 0` or an
  explicit `seed`); embeddings always. Errors/partials never cached; streaming
  bypasses. Bypass via `Cache-Control: no-cache`/`no-store` or
  `x-broker-cache: bypass`; every response carries `x-broker-cache: hit|miss|bypass`.
  Covers `/v1/chat/completions`, `/v1/completions`, `/v1/responses` (non-stream),
  `/v1/embeddings`. Semantic cache deferred.
- **Admin runtime control** (ADR 0012, `BROKER_ADMIN_ENABLED`, default off): behind
  a dedicated `BROKER_ADMIN_API_KEYS` gate (`require_admin`). `GET /admin/models`
  (state, inflight, last-used), `POST /admin/models/{alias}/unload` (refuse on
  in-flight, or `?drain=true` to drain first), `POST /admin/models/{alias}/load`
  (explicit preload — the only non-request load path), `POST /admin/registry/reload`
  (transactional hot-reload: validate-before-apply, drain changed/removed models,
  invalid registry → no change + structured error). Admin actions are audit-logged.
- **OpenTelemetry tracing** (ADR 0012, `BROKER_TRACING_ENABLED`, default off):
  optional `otel` extra (`pip install 'backend-agnostic-broker[otel]'`). Spans
  (`auto_route`→`load_model`→`inference`→`cache_lookup`) carry `gen_ai.*`
  attributes per the GenAI semantic conventions; **no prompt/response content**.
  OTLP exporter configured via standard `OTEL_*` env vars. No-op + zero overhead
  when disabled; `ConfigError` if enabled but the package is missing.
- **Web UI/playground**: `BROKER_UI_ENABLED` reserved; the UI itself is **deferred**
  (ADR 0012, roadmap item 12).

### Changed

- `GET /status` now reports `state_backend`, `retry_max_attempts`,
  `cooldowns_enabled`, a `cache_aware` block (`enabled`, and when active
  `index_size`/`lookups`/`hits`/`hit_rate`/`block_chars`/`beta`), `tracing_enabled`,
  a `response_cache` stats block, and (when cooldowns are on) per-model `cooldown`.
- `GET /healthz/backends` entries now include a per-member `cooldown` object.
- `GET /usage` responses now include a `scope` field and (for `user` scope) the
  caller's `api_key_id`; `admin` scope adds a `by_key` breakdown.

### Security

- **Auth no longer 500s on a non-ASCII API key.** `hmac.compare_digest` raises
  `TypeError` on a non-ASCII `str`, so a request presenting a key with a non-ASCII
  (e.g. latin-1) byte in `Authorization`/`X-API-Key` previously returned **500**
  instead of **401** on the unauthenticated-reachable auth path (pre-existing in
  v0.4.0 `broker/auth.py`, and the same class in the new virtual-key verification).
  Both paths now compare the UTF-8/surrogateescape **byte** encodings (and reject
  absurdly long keys early), so malformed keys fail closed with a clean `401`.
  Regression tests cover non-ASCII and over-long keys on the data-plane auth path.

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
