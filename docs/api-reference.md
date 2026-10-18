# API Reference

All routes are served by `broker/app.py`. The API is OpenAI-compatible for
`/v1/models` and `/v1/chat/completions`, plus operational endpoints. Examples
use `my-key` as a placeholder API key and `echo-fake` as a model alias.

## Authentication

When `BROKER_AUTH_ENABLED=true` (default), every route **except** `/healthz`,
`/readyz`, and `/metrics` requires a valid API key, presented as either:

- `Authorization: Bearer <key>`, or
- `X-API-Key: <key>`

Keys are compared in constant time against `BROKER_API_KEYS`. Missing/invalid
keys return `401` with error type `authentication_error`.

## Common behaviors

- **Request ID** — every response carries `x-request-id` (echoes an inbound
  `x-request-id` or generates `req-<hex>`). It is also included in error bodies.
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
(400), `security_error` (400), `authentication_error` (401), `model_not_found`
(404), `model_load_error` (503), `backend_unavailable` (502), `timeout_error`
(504), `rate_limit_error` (429), `request_too_large` (413).

## Endpoints

### `GET /healthz` — liveness (no auth)

Returns `200` whenever the process is up.

```bash
curl -s http://127.0.0.1:8080/healthz
# {"status":"ok","version":"0.1.0"}
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
`status` of `ok` or `degraded`.

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
`healthy`/`last_health`, public `model` name) and adapter status (endpoint, GPU
allocation), plus a `pools` map (public name → member aliases) and loaded/limit
counts.

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
capabilities ANDed and limits min-ed across members.

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

If `model` names a **pool** (multiple registry members), the broker routes to the
least-busy healthy member and **fails over** to another member if one is
unavailable (before the first byte for streams). The response `model` is the
public name you requested. There is no silent fallback to a *different* model.
See [routing-and-health.md](routing-and-health.md).

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

## OpenAPI

A generated OpenAPI document is at `docs/openapi.yaml` (regenerate with
`python scripts/gen_openapi.py`).
