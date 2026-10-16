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
or `reject`; `gpu_memory_fraction` (per model) must be in `(0, 1]`.

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

## Secrets

Never commit a filled-in `.env`. Provide `BROKER_API_KEYS` via a Docker secret
or env injection. `.gitignore` excludes `.env`.
