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
| Sharpen `auto` routing | refine per-model `tags`/`priority`/`cost_tier`, add `router.overrides`, or set `BROKER_ROUTER_BACKEND=heuristic`\|`llm` |
| Retry transient backend failures | `BROKER_RETRY_MAX_ATTEMPTS>0` + `BROKER_RETRY_BACKOFF_BASE_MS>0` (full-jitter backoff; retries stay within the pool, bounded by the request timeout) |
| Bench flapping backends | `BROKER_COOLDOWN_FAILURES`/`BROKER_COOLDOWN_SECONDS` (circuit breaker; default-off) |

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

### Retries, cooldowns & circuit breaking (ADR 0009)

- **Retries** (`BROKER_RETRY_MAX_ATTEMPTS`): a request retries within its pool up
  to the cap, sleeping `uniform(0, min(MAX_MS, BASE_MS·2^n))` between attempts
  (full jitter). Retrying never crosses to a different model and is bounded by
  `BROKER_REQUEST_TIMEOUT_SECONDS`. Defaults (`0`) = one failover pass, no sleep.
- **Cooldowns** (`BROKER_COOLDOWN_FAILURES`): after N consecutive failures a
  member is benched for `BROKER_COOLDOWN_SECONDS` and skipped; it half-opens for a
  trial after the window (success closes it, failure re-benches). Watch
  `cooldown.in_cooldown`/`seconds_remaining` in `GET /healthz/backends`. If every
  member is benched the pool falls through so it can still self-heal. Disable with
  `BROKER_COOLDOWN_FAILURES=0`.

## Multi-replica / shared state (ADR 0009)

By default each broker keeps routing state (sticky map, cooldowns, counters)
**in-process**, so one replica is fully self-contained. To run several replicas
behind a load balancer with one consistent view:

1. Install the extra: `pip install 'backend-agnostic-broker[redis]'` (or use an
   image built with it).
2. Point every replica at the same Redis: `BROKER_REDIS_URL=redis://<host>:6379/0`.
   Give each broker *group* its own `BROKER_REDIS_PREFIX` if they share one Redis.
3. Optionally set `BROKER_ROUTE_STICKY_TTL_SECONDS` to bound Redis sticky memory
   (in-process uses the LRU `BROKER_ROUTE_STICKY_MAX_ENTRIES` instead).

Verify with `GET /status` → `state_backend: "redis"`. If `BROKER_REDIS_URL` is set
but `redis` is not installed the broker **fails fast** (no silent downgrade).
Operationally, Redis then becomes a shared dependency: monitor it, and note that a
Redis outage degrades cross-replica routing consistency (a single replica with no
Redis URL is unaffected). Keep connection URLs/credentials in env/secret stores —
never in committed files.

## Logging

- `BROKER_LOG_FORMAT=json` (default) emits structured logs (`ts`, `level`,
  `logger`, `msg`); `text` for human-readable. Set `BROKER_LOG_LEVEL` as needed.
- Admin actions (ADR 0012) are logged by the `broker.admin` logger with the
  action, target, and the actor's non-reversible key fingerprint.

## Response cache (ADR 0012)

Off by default. Enable with `BROKER_RESPONSE_CACHE_ENABLED=true` to serve
identical requests from cache (StateStore-backed; shared across replicas when
`BROKER_REDIS_URL` is set). Tune `BROKER_RESPONSE_CACHE_TTL_SECONDS`,
`_MAX_ENTRIES`, and `_MAX_BYTES`. Watch effectiveness in `GET /status` →
`response_cache` (`hits`, `misses`, `hit_rate`, `live_entries`). To force a fresh
answer for one request, send `Cache-Control: no-cache` or `x-broker-cache: bypass`
and check the `x-broker-cache` response header. By default only deterministic
(`temperature == 0`/seeded) requests are cached; set
`BROKER_RESPONSE_CACHE_DETERMINISTIC_ONLY=false` to cache all (accepting that a
sampled answer will be replayed).

## Runtime control (ADR 0012)

Enable with `BROKER_ADMIN_ENABLED=true` and set `BROKER_ADMIN_API_KEYS` (a
dedicated admin key list, distinct from `BROKER_API_KEYS`). Then:

- **Free VRAM now:** `POST /admin/models/{alias}/unload` (add `?drain=true` to let
  in-flight requests finish first, bounded by `BROKER_ADMIN_DRAIN_TIMEOUT_SECONDS`;
  a bare unload is refused with `409 model_busy` while serving).
- **Hide cold-start:** `POST /admin/models/{alias}/load` to preload a model before
  traffic (the only non-request load path; startup still loads nothing).
- **Edit the registry without a restart:** change `registry.yaml`, then
  `POST /admin/registry/reload`. It validates the whole file first — an invalid
  registry changes nothing and returns a structured error. Added/removed/changed
  models are reported; changed and removed models are drained before replacement;
  unchanged models keep serving. Confirm with `GET /admin/models`.

## Tracing (ADR 0012)

Off by default with zero overhead. To enable: install the extra
(`pip install 'backend-agnostic-broker[otel]'`), set `BROKER_TRACING_ENABLED=true`,
and point the OTLP exporter at your collector with the standard env vars, e.g.
`OTEL_EXPORTER_OTLP_ENDPOINT=http://<collector>:4318`. Spans
(`auto_route`→`load_model`→`inference`→`cache_lookup`) carry `gen_ai.*` metadata
only — **never prompt or response content**. If tracing is enabled but the
`opentelemetry` package is missing, the broker fails fast at startup. Prometheus
`/metrics` is unaffected.

## Security operations

- Keep `BROKER_ALLOWED_EXEC_DIRS` and `BROKER_ALLOWED_MODEL_ROOTS` tightly
  scoped. Rotate `BROKER_API_KEYS` via your secret store. Rebuild the image
  periodically to pick up base-image CVE fixes (see `docs/EVIDENCE.md` gate 19).

## Virtual key lifecycle (ADR 0008)

Per-key governance is opt-in via `BROKER_KEYS_PATH` (a git-ignored `keys.yaml`).
Manage keys with the CLI (run from the worktree / container):

```bash
# Create a limited key — the secret is printed ONCE; store it immediately.
python -m broker.keys create --label team-alpha --tenant alpha --scope user \
    --rpm 60 --rpm-burst 10 --tpm 100000 --max-concurrency 4 \
    --daily-tokens 1000000 --monthly-tokens 20000000 \
    --daily-cost 5 --monthly-cost 100 --allowed-models chat,auto \
    --placement local --per-model chat:30:50000

python -m broker.keys list                 # ids/labels/scopes only — no secrets
python -m broker.keys rotate brk_1a2b3c4d --grace-days 7  # successor + grace overlap
python -m broker.keys revoke brk_1a2b3c4d  # disables without deleting
```

- **Rotation (ADR 0016):** `broker.keys rotate <id> --grace-days N` (or
  `POST /admin/keys/{id}/rotate`) mints a successor inheriting the predecessor's
  label/tenant/scope/limits, prints the new secret **once**, and keeps the old key
  valid for `min(its existing expiry, today + N)` days (`0` = revoke immediately) so
  callers cut over with no downtime. `N` must be an integer in `0..365`. Rotation
  **never extends** a key's life and **never resurrects** a dead one: rotating a
  revoked or already-expired key is refused with `409 key_not_active` (CLI: a
  non-zero exit + `error: cannot rotate an inactive key`) — create a fresh key
  instead. Secrets are never recoverable — only hashes are stored.
- **Admin key API (ADR 0016):** with `BROKER_ADMIN_ENABLED=true` and an admin-scope
  key, `GET/POST /admin/keys`, `POST /admin/keys/{id}/rotate`, and
  `DELETE /admin/keys/{id}` manage keys over HTTP; mutations rewrite `keys.yaml` and
  reload the in-memory store. Create/rotate inputs are validated (`400` for a bad
  `grace_days`/`expiry`/`tenant`/`label`); a body over `BROKER_MAX_REQUEST_BYTES`
  is `413`.
- **Durable, concurrency-safe writes (ADR 0016):** every `keys.yaml` mutation
  (admin API and CLI) takes an exclusive `flock` on a `keys.yaml.lock` sidecar for
  the whole read-modify-write and swaps the file atomically (temp file in the same
  dir + `fsync` + `os.replace`). New files are created `0600` (a stricter existing
  mode is preserved). So concurrent admin calls and CLI runs — on one host or
  several replicas sharing the file — never lose an update or expose a torn file.
  Add `keys.yaml.lock` to any backup/ignore rules (it is git-ignored here).
- **Corrupted keys file:** if a reload hits an unparsable `keys.yaml` (hand-edit
  gone wrong, partial copy), the broker **keeps the last good key set in memory**
  and logs an error rather than dropping every key — fail closed for new keys, never
  flip auth open or shut everyone out. Fix the file and it reloads on the next
  mutation; at **startup** an unparsable file still fails fast (`config_error`).
- **Multi-replica:** the admin API/CLI writes the local `keys.yaml`. Rotation and
  revocation take effect on a replica only after that replica re-reads the file, so
  either point every replica at one shared `keys.yaml` (the `flock` makes concurrent
  writers safe) or designate one node as the key-management writer and reload the
  others (restart or next local mutation). There is **no** cross-replica cache
  invalidation for the key set over Redis today — treat the file as the source of
  truth and plan rollouts accordingly.
- **Expiry:** set `--expiry YYYY-MM-DD` (UTC, ISO date/datetime; past dates are
  rejected); expired keys fail closed.
- **Rate-limit algorithm (ADR 0016):** `BROKER_RATE_LIMIT_ALGO=gcra` switches rpm to
  an atomic token-bucket with per-key burst (`limits.rpm_burst`);
  `BROKER_RATE_LIMIT_TPM_ALGO=sliding` switches tpm to a weighted sliding-window
  counter. Defaults (`sliding_log`/`fixed`) reproduce v0.5.0. A governance store
  error **fails closed** (requests are denied) — if Redis is down with governance
  on, expect 5xx/denied rather than an uncapped free-for-all.
- **Cost budgets** require a registry `pricing` block (USD/million tokens); keys
  without priced models only accrue token budgets.
- **Tenant budgets (ADR 0016):** a `tenants:` block in `keys.yaml` caps aggregate
  token/cost across all keys of a tenant, enforced alongside per-key budgets.
- **Counters & resets:** `rpm`/`tpm` reset on a rolling
  `BROKER_RATE_LIMIT_WINDOW_SECONDS` window; token/cost budgets reset at the UTC
  day/month boundary automatically (period-bucketed `StateStore` keys with TTL).
- **Multi-replica caveat:** with Redis shared state a replica that crashes
  mid-request can leak a `max_concurrency` slot until the next successful release;
  if concurrency admission seems stuck, inspect/clear the `gov:conc:<id>` counter.
- **Reporting:** `GET /usage` is scoped — a `user` key sees only its own spend; an
  `admin` key sees all keys plus a `by_key` breakdown.
