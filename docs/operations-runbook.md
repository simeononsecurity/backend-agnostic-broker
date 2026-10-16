# Operations Runbook

## Start / stop

- Local: `make run` (expects `registry.yaml` + env). The broker starts with **no
  models loaded**.
- Docker: `docker compose up -d --build`; `docker compose down -v`.
- Graceful shutdown drains the idle sweeper and stops all broker-managed
  processes within `BROKER_SHUTDOWN_TIMEOUT_SECONDS`.

## Health & readiness

- `GET /healthz` — liveness (always `200` if the process is up). Used by the
  container `HEALTHCHECK`.
- `GET /readyz` — readiness (config + registry loaded).
- `GET /status` (auth) — per-model lifecycle state, inflight count, pid, idle
  seconds, last error, and GPU allocation; plus `loaded_count` and limits.
- `GET /metrics` — Prometheus (requests, latency, in-flight, loaded models).

## Tuning

| Goal | Lever |
|------|-------|
| Keep models warm longer | raise `BROKER_MODEL_IDLE_TIMEOUT_SECONDS` or per-model `idle_timeout_seconds` |
| Hold more models at once | raise `BROKER_MAX_LOADED_MODELS` (watch VRAM/RAM) |
| More total throughput | raise `BROKER_MAX_CONCURRENT_REQUESTS` |
| More per-model concurrency | raise `BROKER_PER_MODEL_MAX_CONCURRENCY` |
| Shed load instead of queueing | `BROKER_LOAD_CONCURRENCY_POLICY=reject` (429 when saturated) |
| Faster idle reclaim | lower `BROKER_IDLE_SWEEP_INTERVAL_SECONDS` |
| Slow-loading runtimes | raise `startup_timeout_seconds` / `readiness.timeout_seconds` |

## Capacity model

- Loading a model past `max_loaded_models` evicts the **least-recently-used
  idle** model. If none are idle (all serving), the request fails with `429
  rate_limit_error` — raise the cap or retry.
- Each broker-managed backend model = one child process holding GPU/RAM while
  resident.

## Handling failures

- A model that fails to start transitions to `failed`, its process is torn down,
  and the request returns `503 model_load_error` with remediation. The next
  request retries from `unloaded`.
- Backend 4xx/5xx surface as `502 backend_unavailable` (with a truncated upstream
  message); timeouts as `504 timeout_error`.
- Inspect `GET /status` for `last_error` and pid/running state; check runtime
  logs (broker surfaces recent child logs on startup failures).

## Logging

- `BROKER_LOG_FORMAT=json` (default) emits structured logs (`ts`, `level`,
  `logger`, `msg`); `text` for human-readable. Set `BROKER_LOG_LEVEL` as needed.

## Security operations

- Keep `BROKER_ALLOWED_EXEC_DIRS` and `BROKER_ALLOWED_MODEL_ROOTS` tightly
  scoped. Rotate `BROKER_API_KEYS` via your secret store. Rebuild the image
  periodically to pick up base-image CVE fixes (see `docs/EVIDENCE.md` gate 19).
