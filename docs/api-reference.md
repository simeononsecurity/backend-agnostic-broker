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

**Governance v2 (ADR 0016).** The same contract extends to:

- **Per-model limits** — `limits.per_model: {model: {rpm, tpm}}` yields `429`
  `rate_limit_exceeded` (message names the model) when a key exceeds a model's cap.
  Enforced on the **resolved** model — the `auto` pick and every cascade hop — so
  it cannot be bypassed via `auto` or an escalation.
- **Tenant budgets** — a `tenants:` block caps aggregate token/cost across all keys
  of a tenant; exhaustion is `429` `insufficient_quota` (message names the tenant),
  enforced in addition to per-key budgets.
- **Per-key placement** — `limits.placement: [labels]` is unioned into the request
  placement requirement (it can only tighten it); an unsatisfiable requirement is
  `403` `placement_denied`, intersecting with the `x-broker-placement` header.
- **Rate-limit algorithm** — `rpm` uses the exact sliding-log by default or an
  atomic **GCRA** token-bucket (`BROKER_RATE_LIMIT_ALGO=gcra`) with per-key burst
  (`limits.rpm_burst`); `tpm` uses a fixed window by default or a weighted
  **sliding-window counter** (`BROKER_RATE_LIMIT_TPM_ALGO=sliding`). The header
  contract is unchanged.

A governance store error **fails closed** (the request is denied) rather than
silently removing the cap.

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
    cascade path like `tiny->coder`. For a streaming cascade in the default
    `fallback_single` mode it reads `strong (cascade->single: streaming)`; in the
    opt-in `buffer` mode (chat only, ADR 0019) it reads the plan
    `tiny->strong (cascade:buffer)` (the executed hop is in `GET /status`). TTFT is
    recorded for every streaming request and feeds the latency budget (ADR 0019).
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
`capacity_unavailable` (429, memory-aware admission — ADR 0022; carries `reason`
and `holder`), `release_stuck` (503, a runtime did not release its device after
eviction — ADR 0020), `queue_full` (429, the admission queue is full — ADR 0023;
carries `trigger` + `Retry-After`), `queue_timeout` (503, a queued request waited
past `BROKER_QUEUE_MAX_WAIT_SECONDS` — ADR 0023; carries `trigger` + `Retry-After`),
`request_too_large` (413), `upstream_client_error` (the upstream 4xx; see below).

### Admission queue headers + errors (ADR 0023)

When `BROKER_QUEUE_ENABLED` is on, a non-streaming response that waited in the
admission queue carries `x-broker-queue-wait-ms` and `x-broker-queue-trigger`.
A request rejected because the queue is full returns **429** `queue_full`; a
request that waited past the limit returns **503** `queue_timeout`; both carry
`Retry-After` and a `trigger` naming the overload. Per-key **quota** 429s
(`rate_limit_exceeded` / `insufficient_quota`) are never queued — they are a
caller limit, distinct from these server-overload codes. Streaming requests
surface their wait in `GET /status` and traces (SSE headers are flushed before
the pre-first-byte wait); a full/timed-out streaming request returns a JSON error
in the stream.

### Upstream backend errors (ADR 0017)

When a backend *responds* with an HTTP error (status `>= 400`), the broker surfaces
it by the upstream status rather than collapsing everything to `502`:

| Upstream status | Client status | `error.type` | Retry / failover |
|-----------------|---------------|--------------|------------------|
| `400`, `404`, `413`, `422`, other `4xx` | the same `4xx` | `upstream_client_error` | never retried, failed over, or benched — the request itself is bad |
| `408` | `504` | `timeout_error` | retried/failed over as a timeout |
| `429` | `429` | `rate_limit_error` (with `Retry-After`) | immediate failover to a healthy member + throttled member benched; a lone member returns `429` (no sleep) |
| `5xx` (e.g. `500`, `503`) | `502` | `backend_unavailable` | retried/failed over; a `503` + `Retry-After` benches the throttled member and (lone member only) sleep-retries within a bounded budget |

A pure *transport* failure (connect error, read timeout, unreachable) remains
`502 backend_unavailable`. Upstream error messages are sanitized and truncated
before being echoed. Every `Retry-After` wait is clamped to
`BROKER_RETRY_AFTER_MAX_SECONDS` and the request deadline (never unbounded). For a
streaming request the transport status is already `200`; a pre-first-byte upstream
error rides in the terminal SSE error frame with the same `error.type`.

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
(`{failures, in_cooldown, until, seconds_remaining, ejections}`; `ejections` is
the consecutive-ejection count driving exponential growth — ADR 0017).

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
`cooldowns_enabled`, and a `reliability` block (ejection cap, cooldown
growth/decay, single-trial half-open, Retry-After, Redis sticky bound — ADR 0017).

When `BROKER_GPU_TELEMETRY` is on (ADR 0020/0022), a `resources` block is included:
`telemetry` (provider/enabled/poll), measured `devices` (`source: measured`, with
per-PID `processes`), `admission` (declared `budget_mib`, `headroom_mib`, recent
decisions), `release` timeout, any `release_stuck` holders, and — for Ollama —
separately-labeled `reported` daemon memory (never mixed with `measured`). With
telemetry off, `resources.source` is `null` and `devices` is empty.
`GET /healthz/backends` carries the same `resources` block.

```bash
curl -s -H "Authorization: Bearer ***" http://127.0.0.1:8080/status
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
curl -s -H "Authorization: Bearer ***" -H 'content-type: application/json' \
  -d '{"model":"nomic-embed-text","input":"the cat sat on the mat"}' \
  http://127.0.0.1:8080/v1/embeddings
# -> {"object":"list","data":[{"object":"embedding","index":0,"embedding":[...768 floats...]}],
#     "model":"nomic-embed-text","usage":{"prompt_tokens":8,"total_tokens":8}}
```

### `POST /v1/rerank` — rerank (auth)

Cohere/Jina-compatible reranking ([ADR 0013](adr/0013-rerank-endpoint.md)), routed
to a **rerank-capable backend/model** (vLLM or llama.cpp reranker) — not the chat
pipeline and never `auto`. Required: `model`, `query` (non-empty string),
`documents` (non-empty list of strings **or** `{"text": ...}` objects). Optional:
`top_n` (non-negative int; caps the results), `return_documents` (bool, default
`false`). Response: a `rerank` object whose `results` are **sorted by
`relevance_score` descending**; `index` is the **original** document ordinal;
`document` (`{"text": ...}`) appears only when `return_documents` is true (omitted,
never `null`, otherwise).

Routing, pooling, lazy-load, failover, per-key scoping, placement, usage logging,
and response caching all apply. A model without `supports_rerank` returns `400`
(`does not support rerank`); an unknown model `404`. **Ollama and Strata rerank
return `400`** — Ollama has no native rerank endpoint and the broker refuses the
embeddings-cosine substitute; Strata rerank is unverified. The documents count is
bounded by `BROKER_RERANK_MAX_DOCUMENTS` (default 1000) and the body by
`BROKER_MAX_REQUEST_BYTES`.

**Upstream contract (`502`).** The broker is the normalization authority and
validates the backend's reply strictly: every result must carry a non-bool
integer `index` within `[0, len(documents))` that is unique across the response,
and a finite numeric `relevance_score`. A malformed reply — an out-of-range,
negative, duplicate, non-integer, or boolean `index`; a non-numeric or
non-finite (`NaN`/`Infinity`) score; a non-JSON body; or a missing/wrong
`results` array — is treated as a backend contract violation and returns `502`
(`backend_unavailable`). Such a response is **never** partially passed through,
silently coerced, or written to the response cache, and it participates in pool
failover/cooldown like any other backend error. The response `id` is always
broker-generated (`rerank-<request_id>`); the upstream `id` is never echoed.
Returning *fewer* results than documents (e.g. the backend's own `top_n`) is
allowed.

```bash
curl -s -H "Authorization: Bearer ***" -H 'content-type: application/json' \
  -d '{"model":"bge-reranker","query":"What is the capital of France?",
       "documents":["Paris is the capital of France.","Bananas are a fruit."],
       "top_n":2}' \
  http://127.0.0.1:8080/v1/rerank
# -> {"id":"rerank-...","object":"rerank","model":"bge-reranker",
#     "results":[{"index":0,"relevance_score":0.99},{"index":1,"relevance_score":0.01}],
#     "usage":{"total_tokens":16}}
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
`/v1/responses`, `/v1/embeddings`, and `/v1/rerank` carries an **`x-broker-cache`** header:

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
score, e.g. `0.9873`). Only the **last user turn's text** is matched fuzzily —
key/tenant, placement, namespace, resolved model, endpoint, all non-prompt params,
**the system prompt, and the whole conversation history** must match exactly, so a
semantic hit never crosses a scope, system prompt, or history boundary. The layer
stores only when the served model equals the keyed model (a cascade escalation to
another model is not stored).

**Streaming requests** (`stream: true`) now also consult the cache: on a hit the
broker returns a well-formed SSE replay (`chat.completion.chunk` /
`text_completion` / Responses events + `[DONE]`) with the `x-broker-cache` header,
instead of bypassing. Only non-streaming responses *populate* the cache.

### Per-request cache controls (ADR 0014)

| Header | Effect |
|--------|--------|
| `x-broker-cache-ttl: <seconds>` | Clamp this request's stored-entry TTL. Finite, `>= 0`; may only **shorten** the operator TTL (`BROKER_RESPONSE_CACHE_TTL_SECONDS` / `BROKER_SEMANTIC_CACHE_TTL_SECONDS`). `0` = do not store this response. Non-finite (`nan`/`inf`/`-inf`, overflowing literals like `1e400`) or negative → `400`. |
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

### Admin key management (ADR 0016)

Manage virtual keys over HTTP (admin-scope only; same `require_admin` gate). The
secret appears **exactly once**, in the create/rotate response, and is never stored
or logged. Requires `BROKER_KEYS_PATH` (else `400` `keys_file_required`).

- **`GET /admin/keys`** — list keys (id/label/tenant/scope/enabled/expiry/limits).
  **Never** returns secrets or hashes.
- **`POST /admin/keys`** — create. Body: `label?`, `tenant?`, `scope?`
  (`user`|`admin`), `expiry?`, `limits?` (same schema as `keys.yaml`). Returns
  `201` with the key view **plus** a one-time `secret`. Inputs are validated
  (`400` `invalid_request_error`): `label` is a string ≤128 chars from
  `[A-Za-z0-9 ._-@:+/]`; `tenant` is a string ≤64 chars from `[A-Za-z0-9._-]`;
  `expiry` is an ISO date/datetime (a datetime is reduced to its UTC date) and a
  **past** date is rejected (`expiry_in_past`) so a key is never born dead.
- **`POST /admin/keys/{id}/rotate`** — mint a successor inheriting the
  predecessor's settings; body `grace_days?` — an **integer** in `0..365`
  (default 7; `0` = revoke old now). A non-integer/out-of-range `grace_days` is
  `400` `invalid_grace_days`. The old key's expiry is set to
  `min(existing expiry, today + grace_days)` — rotation **never extends** a key's
  life. Returns `201` with the new one-time `secret`. `404` `key_not_found` if
  absent; `409` `key_not_active` if the key is already revoked or expired (a dead
  key is never resurrected — create a fresh key instead). The CLI
  `python -m broker.keys rotate` enforces the identical rules.
- **`DELETE /admin/keys/{id}`** — revoke (sets `enabled:false`). `404` if absent.

All `/admin/keys` routes share the global request-size limit: a body over
`BROKER_MAX_REQUEST_BYTES` is rejected with `413` `request_too_large` before the
route runs. `keys.yaml` writes are atomic (temp file + `fsync` + `os.replace`),
hold an exclusive `flock` on a `keys.yaml.lock` sidecar for the whole
read-modify-write, and create the file `0600` (an existing stricter mode is
preserved), so concurrent admin calls / CLI runs on a shared file can't lose an
update or expose a torn file.

```bash
curl -s -X POST -H "X-Admin-Key: ***" -H 'Content-Type: application/json' \
  -d '{"label":"ci","tenant":"alpha","limits":{"rpm":10,"allowed_models":["chat"]}}' \
  http://127.0.0.1:8080/admin/keys
# -> 201 {"id":"brk_1a2b3c4d","scope":"user",...,"secret":"brk_1a2b3c4d_<shown once>"}
```

## Web UI / playground (ADR 0015)

### `GET /ui` — static operator playground (opt-in; no new API surface)

Served **only** when `BROKER_UI_ENABLED=true`; otherwise `/ui` returns `404`
(no route is registered). It is a hardened, fully-offline single-page app
(vanilla HTML/CSS/JS package data — no CDN, no external URLs, no build step) that
drives the **existing authenticated API**; it adds **no new unauthenticated
endpoints**. The operator pastes an API key (and optionally an admin key) into the
page; keys are held in browser memory only (never `localStorage`/`sessionStorage`/
cookies).

Routes registered when enabled: `GET /ui`, `GET /ui/`, and `GET /ui/{path:path}`
(static assets only, confined to the packaged UI directory; path traversal and
non-allow-listed extensions return `404`).

Every `/ui` response carries strict security headers:

```
Content-Security-Policy: default-src 'self'; base-uri 'none'; frame-ancestors 'none'; object-src 'none'; form-action 'self'
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
X-Frame-Options: DENY
Cache-Control: no-store
```

```bash
# Enable, then load in a browser at http://127.0.0.1:8080/ui
BROKER_UI_ENABLED=true broker
curl -s -D - -o /dev/null http://127.0.0.1:8080/ui   # 200 + the headers above
```

Features: model list (capabilities/pool/candidates), chat playground with SSE
streaming over `fetch()` (shows `x-broker-route`/`x-broker-cache`/rate-limit
headers), completions/responses/embeddings tabs, a rerank tab that hides itself
unless `/v1/rerank` exists, `/status` and scoped `/usage` views, and an admin
panel (list/load/unload/reload, with confirmations) that appears only when an
admin key is supplied. See [web-ui.md](web-ui.md).

## OpenAPI

A generated OpenAPI document is at `docs/openapi.yaml` (regenerate with
`python scripts/gen_openapi.py`).
