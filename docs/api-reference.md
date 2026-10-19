# API Reference

All routes are served by `broker/app.py`. The API is OpenAI-compatible for
`/v1/models` and `/v1/chat/completions`, plus operational endpoints. Examples
use `my-key` as a placeholder API key and `echo-fake` as a model alias.

## Authentication

When `BROKER_AUTH_ENABLED=true` (default), every route **except** `/healthz`,
`/readyz`, and `/metrics` requires a valid API key, presented as either:

- `Authorization: Bearer <key>`, or
- `X-API-Key: <key>`

Keys are compared in constant time. Missing/invalid keys return `401` with error
type `authentication_error`.

### Virtual keys & per-key governance (ADR 0008)

`BROKER_API_KEYS` remain valid and are treated as **unlimited user keys**. When a
`BROKER_KEYS_PATH` file is configured, callers may also present a **virtual key**
(`brk_<id>_<secret>`) resolved against a hashed key store (see
[configuration.md](configuration.md)). A virtual key carries an identity (scope
`user`|`admin`, tenant) and limits enforced **before dispatch** on
`/v1/chat/completions`, `/v1/completions`, `/v1/responses`, and `/v1/embeddings`:

- **Rate limits** — `429` `rate_limit_exceeded` with a `Retry-After` header when a
  key's `rpm`, `tpm`, or `max_concurrency` is exceeded.
- **Budgets** — `429` `insufficient_quota` when a daily/monthly token or cost
  budget is exhausted (retrying won't help until the window resets).
- **Model scoping** — `403` `permission_error` (`code: model_not_allowed`) when a
  key requests a model outside its `allowed_models`; this also applies to the
  model the `auto` router selects (fail-closed).

**Rate-limit headers.** Responses to limited keys carry OpenAI-style headers so
clients can self-pace: `x-ratelimit-limit-requests` / `-remaining-requests` /
`-reset-requests` and the `-tokens` equivalents. `Retry-After` (seconds) is set on
`429` responses. Unlimited keys (legacy `BROKER_API_KEYS`, or virtual keys with no
limits) get no governance headers and are never rate-limited.

## Common behaviors

- **Request ID** — every response carries `x-request-id` (echoes an inbound
  `x-request-id` or generates `req-<hex>`). It is also included in error bodies.
- **Routing controls (ADR 0011)** — chat/completions/responses accept optional
  request headers and echo the resolved path:
  - `x-broker-placement: <labels>` — comma-separated data-residency labels
    (e.g. `local` or `region:eu,trusted`). Only models/pool members carrying
    **all** required labels are eligible (for `auto`, an explicit model, and pool
    members); if none qualify the request returns `403 placement_denied` and the
    broker never spills to a non-compliant model. Unioned with the
    `BROKER_DEFAULT_PLACEMENT` baseline. Invalid labels → `400`.
  - `x-broker-latency-budget-ms: <ms>` — pick a faster capable model when the
    chosen model's estimated latency (usage-log p90 + cold-start penalty) exceeds
    the budget; never overrides the capability/placement filters. Non-positive or
    non-numeric → `400`.
  - `x-broker-route` (**response** header) — the model actually served, or a
    cascade path like `tiny->coder`; for a streaming cascade it reads
    `strong (cascade->single: streaming)`.
- **Request-size limit** — bodies over `BROKER_MAX_REQUEST_BYTES` (default
  2,000,000) return `413 request_too_large`.
- **Timeouts** — each request is bounded by the model's
  `request_timeout_seconds`; exceeding it returns `504 timeout_error`.
- **Concurrency** — a global semaphore (`BROKER_MAX_CONCURRENT_REQUESTS`) and a
  per-model semaphore (`BROKER_PER_MODEL_MAX_CONCURRENCY`) bound in-flight work.
  With `BROKER_LOAD_CONCURRENCY_POLICY=reject`, per-model saturation returns
  `429 rate_limit_error`.

## Structured errors

Errors use an OpenAI-style envelope with broker context (`broker/errors.py`):

```json
{
  "error": {
    "message": "unknown model alias: 'nope'",
    "type": "model_not_found",
    "code": "model_not_found",
    "param": null,
    "backend": null,
    "model": "nope",
    "state": null,
    "remediation": "choose one of: echo-fake",
    "request_id": "req-ab12cd34ef567890"
  }
}
```

Error types and HTTP status: `config_error` (500), `invalid_request_error`
(400), `security_error` (400), `authentication_error` (401), `permission_error`
(403, e.g. `model_not_allowed`), `placement_denied` (403), `model_not_found`
(404), `model_load_error` (503), `backend_unavailable` (502), `timeout_error`
(504), `rate_limit_error` (429), `rate_limit_exceeded` (429, per-key
rpm/tpm/concurrency), `insufficient_quota` (429, per-key budget exhausted),
`request_too_large` (413).

## Endpoints

### `GET /healthz` — liveness (no auth)

Returns `200` whenever the process is up.

```bash
curl -s http://127.0.0.1:8080/healthz
# {"status":"ok","version":"0.3.0"}
```

### `GET /readyz` — readiness (no auth)

`200` with model count when config + registry are loaded; `503` otherwise.

```bash
curl -s http://127.0.0.1:8080/readyz
# {"status":"ready","models_registered":1}
```

### `GET /healthz/backends` — per-backend liveness (auth)

Live, **non-loading** liveness of every backend member. Ollama daemons are probed
via `GET /api/tags`; broker-managed runtimes (vLLM/llama.cpp/Strata) are probed
only while their process is already running — an unloaded managed model reports
`not_loaded` (not a failure, and never a spawn). Returns `200` with an overall
`status` of `ok` or `degraded`. When member cooldowns are enabled (ADR 0009),
each backend entry also carries a `cooldown` object
(`{failures, in_cooldown, until, seconds_remaining}`).

```bash
curl -s -H "Authorization: Bearer my-key" http://127.0.0.1:8080/healthz/backends
# {"status":"ok","backends":[{"alias":"echo-fake","model":"echo-fake","backend":"fake",
#   "state":"ready","status":"ok","detail":"ready","inflight":0}, ...]}
```

A background probe (`BROKER_HEALTH_PROBE_*`) runs the same checks periodically and
marks a `ready` model `failed` if its backend has gone away, so the next request
reloads/reroutes. See [routing-and-health.md](routing-and-health.md).

### `GET /metrics` — Prometheus (no auth)

Prometheus exposition when `BROKER_METRICS_ENABLED=true`; otherwise `404`.
Exposes request counts, latency, in-flight gauge, and loaded-model gauge.

### `GET /status` — lifecycle + GPU (auth)

Per-model snapshot (state, inflight, pid, idle seconds, last error, live
`healthy`/`last_health`, public `model` name, and `cooldown` when enabled) and
adapter status (endpoint, GPU allocation), plus a `pools` map (public name →
member aliases), loaded/limit counts, and routing info: `route_strategy`,
`sticky_sessions`, `state_backend` (`in_process`|`redis`), `retry_max_attempts`,
`cooldowns_enabled`.

```bash
curl -s -H "Authorization: Bearer my-key" http://127.0.0.1:8080/status
```

### `GET /v1/models` — list models (auth)

```bash
curl -s -H "Authorization: Bearer my-key" http://127.0.0.1:8080/v1/models
```

Returns `{"object":"list","data":[ ... ]}` — **one entry per public model name**
(a load-balancing pool is collapsed to a single entry). Each entry includes `id`
(public name), `backend`, `capabilities` (streaming/tools/vision), `context_limit`,
`output_limit`, and `pool`/`pool_size`/`pool_members`. Standalone models also carry
`runtime_model_id`, `gpu`, and `mtp`; pooled entries report `backend: "pool"` with
capabilities ANDed and limits min-ed across members. When the auto-router is
enabled, a virtual `auto` entry (`backend: "router"`, with `router_candidates`) is
also listed — send `"model": "auto"` to have a classifier pick the model. See
[auto-router.md](auto-router.md).

### `GET /api/tags` — Ollama-style discovery (auth, optional compat)

Returns `{"models":[{"name","model","details":{"family","context_length"}}]}`
for clients that speak Ollama's discovery contract.

### `GET /api/ps` — Ollama residency passthrough (auth)

Aggregates Ollama's `/api/ps` across all configured Ollama daemons (deduplicated
by base URL) — the models **actually resident in memory**, distinct from the
broker's lifecycle state. Non-loading. Returns `{"daemons":[{"base_url", "models"|"error"}]}`;
empty `daemons` when no Ollama backends are configured.

### `POST /v1/chat/completions` — chat (auth)

Request body is OpenAI-shaped. Required: `model`, `messages`. Optional:
`stream`, `temperature`, `max_tokens`, `tools`, `tool_choice`; unknown OpenAI
fields are passed through to the backend.

If `model` names a **pool** (multiple registry members), the broker routes it per
`BROKER_ROUTE_STRATEGY`. The default **`sticky`** pins each session to one member
and only switches on failure; set `X-Session-Id: <conversation-id>` (or send the
OpenAI `user` field) to control the pairing — otherwise the client IP is used.
Routing **fails over** to another member if one is unavailable (before the first
byte for streams) and re-pins the session. The response `model` is the public
name you requested. There is no silent fallback to a *different* model. See
[routing-and-health.md](routing-and-health.md).

Non-streaming:

```bash
curl -s -H "Authorization: Bearer my-key" -H 'content-type: application/json' \
  -d '{"model":"echo-fake","messages":[{"role":"user","content":"Hello!"}]}' \
  http://127.0.0.1:8080/v1/chat/completions
```

Response is an OpenAI `chat.completion` object (`object:"chat.completion"`,
`choices[]`, `usage`).

Streaming (Server-Sent Events):

```bash
curl -N -H "Authorization: Bearer my-key" -H 'content-type: application/json' \
  -d '{"model":"echo-fake","messages":[{"role":"user","content":"Hello!"}],"stream":true}' \
  http://127.0.0.1:8080/v1/chat/completions
```

Streaming responses are `text/event-stream`, emitting `data: {chunk}\n\n`
`chat.completion.chunk` events and terminating with `data: [DONE]\n\n`. On a
mid-stream error the broker emits a structured error chunk followed by `[DONE]`.

### `POST /v1/completions` — legacy text completions (auth)

Translated to the chat pipeline (so pooling, lazy-load, failover, and the `auto`
router all apply — see [ADR 0006](adr/0006-openai-endpoint-surface.md)). Required:
`model`, `prompt` (string or list of strings). Optional `stream` + any chat
params. Returns an OpenAI `text_completion` object (`choices[].text`, `usage`);
streaming emits `text_completion` chunks + `[DONE]`.

```bash
curl -s -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"model":"echo-fake","prompt":"Hello!"}' \
  http://127.0.0.1:8080/v1/completions
```

### `POST /v1/responses` — OpenAI Responses API (auth)

Translated to the chat pipeline ([ADR 0006](adr/0006-openai-endpoint-surface.md)).
Required: `model`, `input` (string or message list). Optional `instructions`
(prepended as a system message), `max_output_tokens` (mapped to `max_tokens`),
`stream`. Returns a `response` object (`output[]`, `output_text`, `usage` with
`input_tokens`/`output_tokens`); streaming emits `response.output_text.delta`
events then `response.completed` + `[DONE]`.

```bash
curl -s -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"model":"echo-fake","input":"Hello!"}' \
  http://127.0.0.1:8080/v1/responses
```

The text-first shim flattens content parts to text; multimodal/tool fidelity is
best-effort. The no-silent-fallback guarantee holds (unknown model → 404).

### `POST /v1/embeddings` — embeddings (auth)

Routed to an **embedding-capable backend/model** (not the chat pipeline — there is
nothing to generate). Required: `model`, `input` (string or list of strings).
Ollama models are translated to the native `/api/embed`; OpenAI-compatible
runtimes (vLLM, llama.cpp) are proxied to their `/v1/embeddings`. Returns an
OpenAI `list` of `embedding` objects. Routing, pooling, lazy-load, and failover
apply; a chat-only model returns `400` (`does not support embeddings`), an unknown
model `404`.

```bash
curl -s -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"model":"nomic-embed-text","input":"the cat sat on the mat"}' \
  http://127.0.0.1:8080/v1/embeddings
# -> {"object":"list","data":[{"object":"embedding","index":0,"embedding":[...768 floats...]}],
#     "model":"nomic-embed-text","usage":{"prompt_tokens":8,"total_tokens":8}}
```

### `GET /usage` — request/token usage (auth)

Aggregated usage from the usage log ([ADR 0007](adr/0007-usage-accounting-store.md)):
total requests + tokens, per-model and per-endpoint counts, and the most recent
rows (`?limit=N`, default 20). Metadata only — no request/response content, and
the API key is stored as a non-reversible id. Returns `{"enabled": false}`
when the log is disabled.

**Scoping (ADR 0008).** The response is scoped to the caller: a `user`-scope key
sees only its own usage (`scope: "key"`, plus its `api_key_id`); an `admin`-scope
key sees all usage (`scope: "all"`) and a `by_key` per-key spend breakdown. With
auth disabled, the caller is treated as admin.

### Response cache header (ADR 0012)

When the exact response cache is enabled (`BROKER_RESPONSE_CACHE_ENABLED`), every
non-streaming response on `/v1/chat/completions`, `/v1/completions`,
`/v1/responses`, and `/v1/embeddings` carries an **`x-broker-cache`** header:

- `hit` — served from cache (no inference ran);
- `miss` — served fresh (and stored if eligible);
- `bypass` — the cache was skipped for this request.

Bypass the cache per request with `Cache-Control: no-cache` (or `no-store`,
`max-age=0`) or `x-broker-cache: bypass`. The cache is keyed per API-key/tenant
scope, so a cached answer never crosses a key boundary; only deterministic
requests (`temperature == 0` or an explicit `seed`) are cached by default; errors
are never cached. See [configuration.md](configuration.md#response-cache-adr-0012).

### Semantic cache header + streaming replay (ADR 0014)

When the semantic cache is enabled (`BROKER_SEMANTIC_CACHE_ENABLED`), a response
served from the embedding-similarity layer (after an exact miss) carries
`x-broker-cache: semantic-hit` plus **`x-broker-cache-similarity`** (the cosine
score, e.g. `0.9873`). Only the prompt text is matched fuzzily — key/tenant,
placement, namespace, resolved model, endpoint, and all non-prompt params must
match exactly, so a semantic hit never crosses a scope.

**Streaming requests** (`stream: true`) now also consult the cache: on a hit the
broker returns a well-formed SSE replay (`chat.completion.chunk` /
`text_completion` / Responses events + `[DONE]`) with the `x-broker-cache` header,
instead of bypassing. Only non-streaming responses *populate* the cache.

### Per-request cache controls (ADR 0014)

| Header | Effect |
|--------|--------|
| `x-broker-cache-ttl: <seconds>` | Override this request's stored-entry TTL (`>= 0`; `0` = no TTL). |
| `x-broker-cache-namespace: <token>` | Partition the caller's cache scope (rotate to invalidate). Charset: letters/digits/`.`/`-`/`_`/`:`. |
| `Cache-Control: max-age=0` | Bypass (same as `no-cache`). |

`s-maxage` and request `max-age>0` freshness directives are **rejected** (ignored):
the broker keeps no stored-age and has no revalidation protocol — use
`x-broker-cache-ttl` instead.

## Admin runtime control (ADR 0012)

Privileged control plane, **off by default** (`BROKER_ADMIN_ENABLED=true`) and
gated by a dedicated admin key (`X-Admin-Key`, or `Authorization: Bearer`, matched
against `BROKER_ADMIN_API_KEYS` — separate from the normal API keys). Every action
is audit-logged. When admin is disabled the routes return a structured
`admin_disabled` error.

### `GET /admin/models` — runtime snapshot (admin)

Per-model state, in-flight count, loaded-at, idle seconds, healthy, draining.
Non-loading.

```bash
curl -s -H "X-Admin-Key: ***" http://127.0.0.1:8080/admin/models
# -> {"models":[{"alias":"echo","model":"echo","backend":"fake","state":"ready",
#      "inflight":0,"loaded":true,"idle_seconds":3.1,"healthy":true,"draining":false}, ...]}
```

### `POST /admin/models/{alias}/unload` — unload (admin)

Refuses with `409 model_busy` if the model has in-flight requests. Pass
`?drain=true` to stop selecting the member, wait (bounded by
`BROKER_ADMIN_DRAIN_TIMEOUT_SECONDS`) for in-flight to finish, then unload.

```bash
curl -s -X POST -H "X-Admin-Key: ***" \
  "http://127.0.0.1:8080/admin/models/echo/unload?drain=true"
```

### `POST /admin/models/{alias}/load` — explicit preload (admin)

Loads a model without an inference request — the **only** non-request load path
(the lazy-load invariant for the data path is otherwise preserved).

### `POST /admin/registry/reload` — registry hot-reload (admin)

Re-reads and validates the registry and applies the diff with no restart.
**Transactional**: an invalid registry makes **no change** and returns a
structured `config_error`; changed/removed models are drained then replaced;
unchanged models keep serving; added models are registered (not loaded).

```bash
curl -s -X POST -H "X-Admin-Key: ***" http://127.0.0.1:8080/admin/registry/reload
# -> {"added":["echo3"],"removed":["echo2"],"changed":[],"unchanged":["echo"],"models":2}
```

## OpenAPI

A generated OpenAPI document is at `docs/openapi.yaml` (regenerate with
`python scripts/gen_openapi.py`).
