# ADR 0012: Response cache, runtime control/hot-reload, and tracing (+ optional UI)

- Status: Accepted
- Date: 2026-10-19
- Supersedes: none
- Extended by: ADR 0014 (semantic cache, streaming replay, per-request cache
  controls, opt-in span content — resolves this ADR's deferred items)
- Related: ADR 0009 (StateStore — the cache's backing store); ADR 0008 (per-key
  governance — the admin-scope seam); `docs/roadmap.md` items 10/11/12;
  research: `docs/research/adr-0012-caching-runtime-control-observability.md`.
  Prior art: LiteLLM caching, OpenRouter caching, RFC 9111, llama-swap runtime
  control, OpenTelemetry GenAI semantic conventions.

## Context

Three independent ops/experience gaps vs peer LLM gateways, grouped here because
each is small and they share the same invariants and admin surface:

1. **Response caching** — no cache today; identical prompts re-run inference.
   LiteLLM/OpenRouter show large latency/cost wins from exact caching.
2. **Runtime control** — no way to unload a model, explicitly preload one, or
   reload the registry without a restart; llama-swap exposes manual unload +
   list-running + file-watch reload.
3. **Observability depth** — we emit Prometheus + `/status`, but no distributed
   tracing, and no built-in UI/playground.

Hard constraints from our invariants: **no model loads at startup or on a health
check** (lazy load); **no silent cross-model fallback**; new behavior is
**opt-in and default-off** (defaults must reproduce v0.4.0); optional deps go in
a pyproject extra, import-guarded with a `ConfigError` when enabled-but-missing;
**no prompt/response content leaves the broker** without explicit opt-in. The
research note surveys the converging prior art and records what we adopt/reject.

## Decision

Three capabilities, all **opt-in with defaults that reproduce v0.4.0**, plus a
deferred UI.

### 1. Exact response cache (`BROKER_RESPONSE_CACHE_ENABLED`, default off)

- **Key = SHA-256 of `(caller scope, resolved public model, endpoint, normalized
  request body)`.** The caller scope (API-key fingerprint, extensible to tenant)
  is the FIRST component, so a cached answer can **never cross a key/tenant
  boundary**. The model is the one chosen **after `auto` resolution** (documented
  decision rule: the cache keys on the concrete public model the request will run
  on, so two callers who both hit `auto`→*modelX* share correctly). The body is
  normalized (sorted keys, canonical JSON) and includes sampling params, `tools`,
  `tool_choice`, and `response_format`; routing-only fields (`model`, `stream`,
  `user`) are excluded.
- **Deterministic-only by default** (`temperature == 0` or an explicit `seed`;
  embeddings are always deterministic). Configurable to cache all requests.
- **Never cache errors or partial/streamed responses.** Streaming requests
  **bypass** the cache (replaying an assembled SSE stream from cache is deferred).
- **Bypass** via `Cache-Control: no-cache`/`no-store` (RFC 9111 request
  directives — treated as a full bypass since the broker has no revalidation
  protocol) or `x-broker-cache: bypass`. Every response carries
  `x-broker-cache: hit|miss|bypass`.
- **Backed by the ADR 0009 StateStore** (`kv_*`): in-process by default, or Redis
  (shared across replicas) when `BROKER_REDIS_URL` is set. TTL + per-entry byte
  cap + an LRU `max_entries` bound. Covers chat, completions, responses
  (non-stream), embeddings.
- **Semantic (embedding-similarity) cache is deferred** — it needs an embedding
  model + vector store and "goes badly wrong on agentic" multi-turn traffic.

### 2. Admin runtime control (`BROKER_ADMIN_ENABLED`, default off)

Behind a dedicated `require_admin` gate (see the admin-scope seam below):

- `GET /admin/models` — per-model runtime snapshot (state, inflight, loaded-at,
  idle seconds, healthy, draining). Non-loading.
- `POST /admin/models/{alias}/unload` — unload on demand. Refuses with `409
  model_busy` if there are in-flight requests; `?drain=true` stops selecting the
  member, waits (bounded by `admin_drain_timeout_seconds`) for in-flight to reach
  zero, then unloads (two-phase drain — the standard zero-downtime pattern).
- `POST /admin/models/{alias}/load` — explicit preload. **The only way a model
  loads without an inference request**; the lazy-load invariant for the normal
  data path is untouched (still no load at startup / on health checks).
- `POST /admin/registry/reload` — re-read + validate the registry and apply the
  diff without a restart. **Transactional**: the new registry is fully loaded and
  every adapter config validated *before anything mutates*, so an invalid
  registry makes **no change** and returns a structured `ConfigError`. Unchanged
  models keep their live instance (and loaded state); changed/removed models are
  **drained then stopped**; added models are registered (not loaded).
- Every admin action is audit-logged (action, target, actor key fingerprint).

### 3. OpenTelemetry tracing (`BROKER_TRACING_ENABLED`, default off)

- Optional `otel` extra. When enabled but `opentelemetry` is missing, startup
  fails fast with a `ConfigError`. When disabled, tracing is a **no-op with zero
  overhead** (no SDK import; spans are a null context manager).
- Spans follow the **GenAI semantic conventions**: `auto_route`, `load_model`,
  `inference`, `cache_lookup` carry `gen_ai.operation.name`,
  `gen_ai.request.model`, `gen_ai.provider.name`, `gen_ai.response.model`,
  `gen_ai.usage.input_tokens`/`output_tokens`, and `error.type`.
- **No prompt/response content in spans, ever** (the spec mandates content be
  opt-in; we do not even implement the opt-in here). The OTLP exporter is
  configured entirely via standard `OTEL_*` env vars; the broker sets only the
  service name. Prometheus `/metrics` is unchanged.

### 4. Web UI/playground — DEFERRED

`BROKER_UI_ENABLED` is reserved (default off, currently inert). A hardened static
page (strict CSP, no external CDN, key in memory only) is real work orthogonal to
the three backend features; the admin + cache APIs are the substrate a future UI
would consume. Recorded as deferred here and in the roadmap.

### Admin-scope seam (resolved in integration with ADR 0008)

ADR 0008 introduces virtual keys with `user|admin` scopes. This ADR shipped the
admin surface behind a small, clearly-isolated `require_admin` dependency
(`broker/app.py`) with a minimal `BROKER_ADMIN_API_KEYS` list as its single swap
point. **In the `integration/p1-p2` integration that swap is done:** `require_admin`
now authorizes a request by **either** a virtual key whose scope is `admin`
(resolved through the ADR 0008 key store) **or** a `BROKER_ADMIN_API_KEYS` value
(retained as a documented back-compat source). Both are compared **byte-wise** so a
non-ASCII/over-long admin key fails closed with a clean `401` (not a `500`), and the
admin surface stays locked even when data-plane auth is disabled. Config validation
accepts a `BROKER_KEYS_PATH` (admin-scope key source) as an alternative to
`BROKER_ADMIN_API_KEYS`. Regression tests: `tests/test_admin.py`
(`test_admin_non_ascii_key_401_not_500`, `test_admin_scope_virtual_key_authorizes`,
`test_user_scope_virtual_key_denied_on_admin`).

## Alternatives considered

- **No cache / metrics-only observability** — simplest; leaves the biggest
  repeat-latency/cost win and per-request latency breakdowns unclaimed.
- **Semantic cache now** — rejected: heavy new deps, and fuzzy matches are unsafe
  for multi-turn/agentic traffic. Deferred.
- **File-watcher registry reload** (llama-swap style) — rejected in favor of an
  explicit, audited `POST /admin/registry/reload`: reload becomes an intentional,
  logged, transactional operation instead of a side effect of an editor save, and
  is trivially testable.
- **Reusing the normal API keys for admin** — rejected: the control plane must be
  isolated from the data plane, and ADR 0008 will introduce a real admin scope.
- **Capturing prompt content in spans (even opt-in)** — rejected for now: stays on
  the safe side of the threat model; metadata-only spans are enough for latency
  and routing analysis.

## Consequences

- Caching must never serve across a key/tenant boundary or cache errors/partials;
  the key design (scope-first, resolved-model, normalized body) and
  deterministic-only default are the mitigations, each tested.
- Admin endpoints widen the control surface — gated, audited, and default-off.
  The drain protocol means an unload/reload never drops an in-flight request.
- Tracing adds an optional dependency and a tiny per-span cost only when enabled;
  disabled is byte-for-byte v0.4.0.
- Each item is independently shippable and default-off, so upgrading changes
  nothing until an operator opts in.

## Confirmation

Verified by the test suite (run from the worktree; the `otel` span assertions use
an in-memory exporter and skip cleanly where the optional dep is absent, exercised
green in a private `[dev,otel]` venv):

- `tests/test_cache.py` — cache-key **stability across dict ordering** and
  **isolation across scope / model / endpoint**; sampling/tools/`response_format`
  change the key while `stream`/`user` do not; **deterministic-only default**
  (temperature>0 never hits); **hit/miss/bypass** headers end-to-end; per-API-key
  **isolation** (no cross-key read); **errors are not cached**; embeddings cached;
  disabled = pass-through; StateStore-backed roundtrip, **TTL expiry**,
  **max_entries LRU eviction**, and **max_bytes** skip.
- `tests/test_admin.py` — admin **disabled blocks**; **admin key required** (a
  user key is rejected); model list; **explicit load then unload**; **unload
  refuses on in-flight** and **drain waits / times out**; a **draining member is
  skipped by selection**; registry reload **adds/removes**, **keeps unchanged
  model instances**, and **rolls back on an invalid registry** (no change).
- `tests/test_tracing.py` — **no-op when disabled** (null span, app serves);
  **`ConfigError` when enabled without opentelemetry** (skipped when installed);
  with the SDK present, `inference`/`load_model` spans carry `gen_ai.*`
  attributes and **no span contains prompt/response content**.
- Existing suites stay green unchanged, proving defaults reproduce v0.4.0. Gates:
  `ruff check` + `ruff format --check` + full `pytest`; `docs/openapi.yaml`
  regenerated.
