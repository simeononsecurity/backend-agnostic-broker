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

### `GET /metrics` — Prometheus (no auth)

Prometheus exposition when `BROKER_METRICS_ENABLED=true`; otherwise `404`.
Exposes request counts, latency, in-flight gauge, and loaded-model gauge.

### `GET /status` — lifecycle + GPU (auth)

Per-model snapshot (state, inflight, pid, idle seconds, last error) and adapter
status (endpoint, GPU allocation), plus loaded/limit counts.

```bash
curl -s -H "Authorization: Bearer my-key" http://127.0.0.1:8080/status
```

### `GET /v1/models` — list models (auth)

```bash
curl -s -H "Authorization: Bearer my-key" http://127.0.0.1:8080/v1/models
```

Returns `{"object":"list","data":[ ... ]}` where each entry includes `id`
(alias), `backend`, `runtime_model_id`, `context_limit`, `output_limit`,
`capabilities` (streaming/tools/vision), `gpu`, and `mtp`.

### `GET /api/tags` — Ollama-style discovery (auth, optional compat)

Returns `{"models":[{"name","model","details":{"family","context_length"}}]}`
for clients that speak Ollama's discovery contract.

### `POST /v1/chat/completions` — chat (auth)

Request body is OpenAI-shaped. Required: `model`, `messages`. Optional:
`stream`, `temperature`, `max_tokens`, `tools`, `tool_choice`; unknown OpenAI
fields are passed through to the backend.

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
