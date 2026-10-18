# Configuration Reference

All broker behavior is configured via environment variables (prefix `BROKER_`),
an optional `.env` file, a mounted registry file, or Docker Compose env
injection. Settings are typed and validated at startup (`broker/config.py`);
invalid configuration fails fast with an actionable error. List-valued variables
use JSON syntax, e.g. `BROKER_API_KEYS=["my-key"]`.

Start from `.env.example`.

## Fail-fast validation (`validate_runtime`)

At startup the broker rejects:

- `auth_enabled=true` with no `api_keys` → set keys or disable auth (dev only).
- `port` outside `1..65535`.
- `max_loaded_models < 1`, `max_concurrent_requests < 1`.
- a `registry_path` that does not exist.

`log_format` must be `json` or `text`; `load_concurrency_policy` must be `queue`
or `reject`; `route_strategy` must be `sticky`, `least_busy`, or `round_robin`;
`gpu_memory_fraction` (per model) must be in `(0, 1]`.

## Server

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_HOST` | str | `0.0.0.0` | Bind address. |
| `BROKER_PORT` | int | `8080` | Bind port (1–65535). |
| `BROKER_LOG_LEVEL` | str | `INFO` | Python log level. |
| `BROKER_LOG_FORMAT` | str | `json` | `json` or `text`. |

## Authentication

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_AUTH_ENABLED` | bool | `true` | Require an API key on protected routes. |
| `BROKER_API_KEYS` | list[str] | `[]` | Accepted keys (JSON list). Provide via secret in production. |

## Registry / models

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_REGISTRY_PATH` | str | `registry.yaml` | Path to the model registry (YAML/JSON). |
| `BROKER_DEFAULT_MODEL` | str? | _(none)_ | Optional default alias (must exist in the registry). |
| `BROKER_ENABLED_BACKENDS` | list[str] | `["fake","ollama","vllm","llamacpp","strata"]` | Backends to enable; registry entries for disabled backends are filtered out. |

## Lifecycle / concurrency

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_MODEL_IDLE_TIMEOUT_SECONDS` | float | `300` | Idle unload timeout (global default; per-model override in registry). |
| `BROKER_MAX_LOADED_MODELS` | int | `3` | Max concurrently resident models; loading past this evicts the LRU idle model. |
| `BROKER_MAX_CONCURRENT_REQUESTS` | int | `32` | Global in-flight request cap. |
| `BROKER_PER_MODEL_MAX_CONCURRENCY` | int | `4` | Per-model in-flight cap. |
| `BROKER_REQUEST_TIMEOUT_SECONDS` | float | `300` | Default per-request timeout. |
| `BROKER_STARTUP_TIMEOUT_SECONDS` | float | `120` | Default model-start readiness timeout. |
| `BROKER_SHUTDOWN_TIMEOUT_SECONDS` | float | `30` | Graceful shutdown budget. |
| `BROKER_IDLE_SWEEP_INTERVAL_SECONDS` | float | `5` | Idle-sweep cadence. |
| `BROKER_LOAD_CONCURRENCY_POLICY` | str | `queue` | `queue` (wait) or `reject` (429) when per-model concurrency is saturated. |

## Routing (load balancing across pool members)

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_ROUTE_STRATEGY` | str | `sticky` | `sticky`, `least_busy`, or `round_robin` (see below). |
| `BROKER_ROUTE_STICKY_MAX_ENTRIES` | int | `10000` | Max remembered session→member pairings (LRU-evicted); bounds sticky memory. |

- **`sticky`** (default) — pin each **session** to one pool member and keep using
  it; only switch members if that member is unhealthy/unavailable (failover
  re-pins the session). A session is identified by the `X-Session-Id` request
  header, else the OpenAI `user` field in the body, else the client IP. New
  sessions are assigned the least-busy member, so distinct sessions still spread.
- **`least_busy`** — route every request to the member with the fewest in-flight
  requests (round-robin tie-break).
- **`round_robin`** — rotate members every request.

See [routing-and-health.md](routing-and-health.md).

## Health probing (non-loading)

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_HEALTH_PROBE_ENABLED` | bool | `true` | Run a periodic background liveness probe (never loads a model). |
| `BROKER_HEALTH_PROBE_INTERVAL_SECONDS` | float | `30` | Seconds between probe sweeps. |
| `BROKER_HEALTH_PROBE_TIMEOUT_SECONDS` | float | `5` | Per-probe timeout. |

Probes use non-loading calls (Ollama `GET /api/tags`; managed runtimes only while
running) and mark a `ready` model `failed` if its backend has gone away, so the
next request reloads/reroutes. Surfaced at `GET /healthz/backends` and in
`GET /status`.

## Limits

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_MAX_REQUEST_BYTES` | int | `2000000` | Max request body size (413 if exceeded). |

## Security allow-lists

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_ALLOWED_EXEC_DIRS` | list[str] | `["/usr/bin","/usr/local/bin","/opt"]` | Directories a runtime executable may live in. |
| `BROKER_ALLOWED_MODEL_ROOTS` | list[str] | `["/models"]` | Roots that a model path must resolve inside. |
| `BROKER_EXTRA_ALLOWED_BACKEND_URLS` | list[str] | `[]` | Extra trusted upstream base URLs for the SSRF allow-list (beyond registry `base_url`s). |
| `BROKER_ALLOW_LOOPBACK_BACKENDS` | bool | `true` | Allow upstreams on loopback (local runtimes bind `127.0.0.1`). |

## GPU

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_GPU_VISIBLE` | bool | `false` | Whether GPUs are exposed to the broker/runtimes. |
| `BROKER_CUDA_VISIBLE_DEVICES` | str? | _(none)_ | e.g. `0,1`; sets device visibility for broker-managed runtimes. |

## Cache / tmp

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_CACHE_DIR` | str | `/var/cache/broker` | Writable cache dir (tmpfs/volume in Docker). |
| `BROKER_TMP_DIR` | str | `/tmp/broker` | Writable tmp dir. |

## Observability

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_METRICS_ENABLED` | bool | `true` | Expose `/metrics`. |
| `BROKER_TRACING_ENABLED` | bool | `false` | Reserved for tracing integration. |

## Docker / networking

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_DOCKER_NETWORK_MODE` | str | `bridge` | Documentation/Compose hint for network mode. |

## Registry env-var substitution

The registry file (`BROKER_REGISTRY_PATH`) is expanded against the process
environment **before** it is parsed (`broker/registry.py::_expand_env`). Any
`${VAR}` or `${VAR:-default}` reference is replaced with the environment value;
a referenced variable that is unset **and** has no default raises a fail-fast
`ConfigError`. Only this exact `${...}` syntax is expanded — a literal `$` is
left untouched.

Use it to keep deployment-specific endpoints and host details **out of version
control**: commit `registry.example.yaml` with placeholders and set the real
value in a gitignored `.env`. For example, to target a remote (e.g. tailscale)
Ollama daemon:

```yaml
# registry.yaml  (gitignored)
- alias: remote-ollama
  backend: ollama
  runtime_model_id: qwen2.5:14b-instruct
  base_url: ${OLLAMA_BASE_URL}        # value lives only in .env
```

```bash
# .env  (gitignored)
OLLAMA_BASE_URL=http://<daemon-host>:11434
```

`python -m broker` loads `.env` into the process environment first
(`broker/__main__.py::_load_local_env`, stdlib-only and **non-overriding**), so
non-`BROKER_`-prefixed variables like `OLLAMA_BASE_URL` reach the expander. An
existing environment variable always wins, so Docker/Compose/secret injection
overrides `.env`.

## Secrets

Never commit a filled-in `.env`. Provide `BROKER_API_KEYS` via a Docker secret
or env injection. `.gitignore` excludes `.env`.
