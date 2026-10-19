# Configuration Reference

All broker behavior is configured via environment variables (prefix `BROKER_`),
an optional `.env` file, a mounted registry file, or Docker Compose env
injection. Settings are typed and validated at startup (`broker/config.py`);
invalid configuration fails fast with an actionable error. List-valued variables
use JSON syntax, e.g. `BROKER_API_KEYS=["my-key"]`.

Start from `.env.example`.

## Fail-fast validation (`validate_runtime`)

At startup the broker rejects:

- `auth_enabled=true` with no `api_keys` **and** no `keys_path` file → set keys,
  provide a virtual-keys file, or disable auth (dev only).
- `port` outside `1..65535`.
- `max_loaded_models < 1`, `max_concurrent_requests < 1`.
- `rate_limit_window_seconds <= 0`.
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
| `BROKER_API_KEYS` | list[str] | `[]` | Accepted keys (JSON list). Provide via secret in production. Treated as **unlimited user keys** under per-key governance. |

## Per-key governance (virtual keys, rpm/tpm, budgets) — ADR 0008

**Opt-in.** With no keys file and no per-key limits the broker behaves exactly as
v0.4.0. Virtual keys add per-caller identity, rate limits, budgets, and model
scoping; counters live on the ADR 0009 `StateStore` (in-process or Redis).

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_KEYS_PATH` | str? | `None` | Path to a git-ignored `keys.yaml` of HASHED virtual keys. Manage with `python -m broker.keys`. See `keys.example.yaml`. |
| `BROKER_RATE_LIMIT_WINDOW_SECONDS` | float | `60.0` | Window for per-key `rpm` (sliding log) and `tpm` (windowed counter). |

Each key in `keys.yaml` carries an `id`, `label`, `tenant`, `scope`
(`user`|`admin`), the SHA-256 **hash** of its secret (never the secret), an
`enabled` flag, an optional `expiry` (ISO date, UTC), and a `limits` block — any
subset of: `rpm`, `tpm` (input+output tokens), `max_concurrency`,
`daily_token_budget`, `monthly_token_budget`, `daily_cost_usd`,
`monthly_cost_usd`, and `allowed_models` (fail-closed; applies to the model the
`auto` router selects too). Cost budgets require a registry `pricing` block (see
`docs/model-registry.md` / `registry.example.yaml`). Keys/limits are enforced
before dispatch; see `docs/api-reference.md` for the error/header contract.

Manage keys:

```bash
python -m broker.keys create --label team-alpha --tenant alpha \
    --rpm 60 --tpm 100000 --max-concurrency 4 \
    --daily-tokens 1000000 --daily-cost 5 --allowed-models chat,auto
python -m broker.keys list              # never prints secrets or hashes
python -m broker.keys revoke brk_1a2b3c4d
```

The CLI prints the new secret **exactly once**; only its hash is written.

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
| `BROKER_ROUTE_STRATEGY` | str | `sticky` | `sticky`, `least_busy`, `round_robin`, or `cache_aware` (see below). |
| `BROKER_ROUTE_STICKY_MAX_ENTRIES` | int | `10000` | Max remembered session→member pairings (LRU-evicted); bounds sticky memory. |

- **`sticky`** (default) — pin each **session** to one pool member and keep using
  it; only switch members if that member is unhealthy/unavailable (failover
  re-pins the session). A session is identified by the `X-Session-Id` request
  header, else the OpenAI `user` field in the body, else the client IP. New
  sessions are assigned the least-busy member, so distinct sessions still spread.
- **`least_busy`** — route every request to the member with the fewest in-flight
  requests (round-robin tie-break).
- **`round_robin`** — rotate members every request.
- **`cache_aware`** — prefix-affinity + load-aware scoring (ADR 0010); opt-in.
  Configured by the settings below.

See [routing-and-health.md](routing-and-health.md).

## Cache-aware routing (ADR 0010)

Only active when `BROKER_ROUTE_STRATEGY=cache_aware`; otherwise these settings have
no effect and the broker behaves exactly as v0.4.0.

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_CACHE_AWARE_BETA` | float | `0.25` | Load-penalty weight in `score = matched_fraction - beta*relative_load`. `0` = pure prefix affinity; higher spreads load harder. Default `0.25` is tuned from the `scripts/bench_routing.py` scenario×β matrix (see [routing-and-health.md](routing-and-health.md#cache-aware-routing-adr-0010)); `1.0` over-penalized affinity. |
| `BROKER_CACHE_AWARE_BLOCK_CHARS` | int | `128` | Characters per prefix block (~4 chars/token). Larger = smaller index, coarser matching. Only full blocks are hashed. |
| `BROKER_CACHE_AWARE_MAX_ENTRIES` | int | `10000` | Prefix-index LRU bound per replica. `0` = unbounded (TTL still applies). |
| `BROKER_CACHE_AWARE_TTL_SECONDS` | float | `1800` | TTL on prefix-index entries, approximating KV-cache residency. `0` = no expiry. |
| `BROKER_CACHE_AWARE_USE_METRICS` | bool | `false` | Let the non-loading health probe scrape vLLM `/metrics` (`vllm:num_requests_waiting`) for a real load signal; falls back to in-flight counts. Never loads a model. |

Routes each request to the pool member that already holds its prompt **prefix**
(warm KV cache), backing off to a less-loaded member once the warm one saturates.
The prefix→member index lives on the `StateStore` (shared via Redis when
`BROKER_REDIS_URL` is set). See [routing-and-health.md](routing-and-health.md) for
the algorithm, `beta` tuning, and the benchmark.

## Retries & backoff (ADR 0009)

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_RETRY_MAX_ATTEMPTS` | int | `0` | Total serve attempts per request. `0` = legacy (one attempt per pool member — a single failover pass). A positive value caps attempts and enables re-trying members after a backoff delay. |
| `BROKER_RETRY_BACKOFF_BASE_MS` | float | `0` | Backoff base. `0` = no sleep (legacy). |
| `BROKER_RETRY_BACKOFF_MAX_MS` | float | `2000` | Backoff cap. |

Retries re-select a member **within the requested model's pool only** — never a
different model. The delay between attempts is full-jitter capped exponential
(the [AWS pattern](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/)):
`delay = uniform(0, min(MAX_MS, BASE_MS * 2**attempt))`. Streaming only retries
*before the first byte*; once a stream is emitting it is never retried. Retrying
respects `BROKER_REQUEST_TIMEOUT_SECONDS` as an overall deadline. **Defaults
reproduce v0.4.0 exactly** (one failover pass, no sleep).

## Member cooldowns (ADR 0009)

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_COOLDOWN_FAILURES` | int | `0` | Consecutive failures before a member is benched. `0` = disabled (circuit breaker off). |
| `BROKER_COOLDOWN_SECONDS` | float | `30` | How long a benched member stays out before a half-open trial. |

A circuit breaker per pool member: after `COOLDOWN_FAILURES` consecutive failures
a member is benched for `COOLDOWN_SECONDS` and selection skips it; once the window
elapses the member is eligible again as a half-open trial (a success resets its
failure count, another failure re-benches it). When enabled, cooldown state is the
authoritative benching signal; if every member is benched the pool falls through
so it can still self-heal. Cooldown state is shown in `GET /status` (per model)
and `GET /healthz/backends`. **Default-off preserves v0.4.0.**

## Shared state / Redis (ADR 0009)

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_REDIS_URL` | str? | _(unset)_ | When set, the sticky map, cooldowns, counters, and cached values live in Redis so multiple broker replicas share one view. Unset = in-process (no new dependency). |
| `BROKER_REDIS_PREFIX` | str | `broker` | Key namespace, so one Redis can host several brokers. |
| `BROKER_ROUTE_STICKY_TTL_SECONDS` | float | `0` | TTL for Redis sticky keys (`0` = no expiry). In-process sticky uses `BROKER_ROUTE_STICKY_MAX_ENTRIES` (LRU) instead. |

`redis` is an **optional** dependency: `pip install
'backend-agnostic-broker[redis]'`. If `BROKER_REDIS_URL` is set but the package is
missing, the broker fails fast with a `ConfigError` (never a silent downgrade to
in-process). See [routing-and-health.md](routing-and-health.md) and
[operations-runbook.md](operations-runbook.md).

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

## Auto-router (`auto` alias)

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_ROUTER_ENABLED` | bool | `true` | Expose the virtual `auto` model. |
| `BROKER_ROUTER_ALIAS` | str | `auto` | Model id that triggers classification. |
| `BROKER_ROUTER_BACKEND` | str | `auto` | `auto`, `llm`, `heuristic`, or `off`. |
| `BROKER_ROUTER_MODEL_PATH` | str? | _(unset)_ | GGUF for the llama.cpp classifier (set in-container). |
| `BROKER_ROUTER_MODEL_CTX` | int | `2048` | Classifier context window. |
| `BROKER_ROUTER_THREADS` | int? | _(auto)_ | CPU threads for the classifier. |
| `BROKER_ROUTER_MAX_INPUT_CHARS` | int | `2048` | Last-user-message truncation (~512 tokens). |
| `BROKER_ROUTER_CACHE_SIZE` | int | `512` | Decision cache entries (LRU). |
| `BROKER_ROUTER_TIMEOUT_SECONDS` | float | `10` | Per-classification timeout. |
| `BROKER_ROUTER_DEFAULT_MODEL` | str? | _(unset)_ | Fallback model id (overrides registry `router.default`). |
| `BROKER_ROUTER_LATENCY_BUDGET_MS` | float? | _(unset)_ | Default per-request latency budget (ms); the `x-broker-latency-budget-ms` header then registry `router.latency_budget_ms` win first. See [auto-router.md](auto-router.md). |
| `BROKER_ROUTER_LATENCY_WINDOW_SECONDS` | float | `3600` | Rolling window of the usage log used to estimate per-model latency (p90). |
| `BROKER_ROUTER_COLD_START_MS` | float | `1500` | Estimated extra latency added for a not-currently-resident model (cold start). |

Policy, per-model `tags`/`priority`/`cost_tier`, cascade mode, and hard overrides
live in the registry. See [auto-router.md](auto-router.md).

## Placement / data-residency pinning (fail-closed)

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_DEFAULT_PLACEMENT` | list[str] | `[]` | Baseline placement labels required of **every** request (unioned with the `x-broker-placement` header). Empty = no baseline. |

Per-model `placement` labels live in the registry. A request carrying a placement
requirement (header and/or default) is only served by models/pool members that
carry **all** required labels; if none qualify the broker returns
`403 placement_denied` and never spills to a non-compliant model. See
[auto-router.md](auto-router.md) and [security-model.md](security-model.md).

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

## Response cache (ADR 0012)

Exact-match response cache, **default off**. When enabled, identical requests to
the same model and caller scope are served from the ADR 0009 StateStore
(in-process, or Redis when `BROKER_REDIS_URL` is set) instead of re-running
inference. The key hashes (API-key/tenant scope, public model **after `auto`
resolution**, normalized messages/prompt/input + sampling + `tools` +
`response_format`), so a cached answer **never crosses a key/tenant boundary**.
Errors and partial/streamed responses are never cached; streaming requests
bypass. Per-request bypass via `Cache-Control: no-cache`/`no-store` or
`x-broker-cache: bypass`; every response carries `x-broker-cache: hit|miss|bypass`.

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_RESPONSE_CACHE_ENABLED` | bool | `false` | Master switch for the exact response cache. |
| `BROKER_RESPONSE_CACHE_TTL_SECONDS` | float | `300` | Operator TTL ceiling for stored entries; `0` = no TTL (rely on `max_entries`). A per-request `x-broker-cache-ttl` may only shorten it. |
| `BROKER_RESPONSE_CACHE_MAX_ENTRIES` | int | `1000` | In-process LRU bound on live entries; `0` = unbounded (Redis uses TTL). |
| `BROKER_RESPONSE_CACHE_MAX_BYTES` | int | `1000000` | Per-entry cap; a larger serialized result is not stored. `0` = no cap. |
| `BROKER_RESPONSE_CACHE_DETERMINISTIC_ONLY` | bool | `true` | Cache only deterministic requests (`temperature == 0` or an explicit `seed`); embeddings always. Set `false` to cache all. |

### Streaming replay + per-request controls (ADR 0014)

Streaming requests (`stream: true`) now **consult** the cache and, on a hit, are
served as a well-formed SSE replay instead of bypassing; only non-streaming
responses *populate* the cache. Per request:

| Header | Effect |
|--------|--------|
| `x-broker-cache-ttl: <seconds>` | Clamp this request's stored-entry TTL. Finite, `>= 0`; may only **shorten** the operator TTL. `0` = do not store this response. Non-finite (`nan`/`inf`/`-inf`, overflow like `1e400`) or negative → `400`. |
| `x-broker-cache-namespace: <token>` | Partition the caller's cache scope (rotate to invalidate). |
| `Cache-Control: max-age=0` | Bypass (same as `no-cache`/`no-store`). |

`s-maxage` and request `max-age>0` freshness are rejected (no stored-age /
revalidation protocol).

## Semantic cache (ADR 0014)

Embedding-similarity cache, **default off**. A second layer consulted *after* an
exact miss for chat/completions/responses: it embeds the prompt via the broker's
own `/v1/embeddings` path (`BROKER_SEMANTIC_CACHE_MODEL`, a registry alias) and
serves the closest stored response whose cosine similarity clears the threshold.
**Strict scoping** — key/tenant + placement + namespace + resolved model +
endpoint + every non-prompt param (sampling, `tools`, `response_format`, `seed`,
...) **plus the system prompt and the whole conversation history** must match
exactly; only the **last user turn's text** is fuzzy (and only its text — any
non-text content parts stay exact) — so a semantic hit never crosses a scope,
system prompt, or history boundary. Completions (`prompt`) and responses
(`input` + `instructions`) are normalized to messages upstream, so the same rule
covers all three endpoints. Pure-Python cosine over a bounded per-bucket index
(no numpy); the **number of buckets** is also globally bounded
(`BROKER_SEMANTIC_CACHE_MAX_BUCKETS`, LRU) so a caller varying non-prompt params
cannot grow the index without limit; in-process or Redis via the StateStore. A
semantic hit returns `x-broker-cache: semantic-hit` + `x-broker-cache-similarity`.

The semantic layer stores only when the **served** model equals the model the
request keyed on (the pre-resolution pick); if a cascade escalated to a different
model the response is not stored (it would otherwise be served to a later request
that resolved to the primary). Per-request `x-broker-cache-ttl` follows the same
clamp rules as the exact cache (finite, may only shorten the operator TTL, `0` =
do not store).

**Lazy-load invariant.** With `BROKER_SEMANTIC_CACHE_REQUIRE_RESIDENT=true`
(default) the cache is consulted/populated **only when the embedding model is
already resident**, so a chat request *never silently loads an extra model*. Warm
it with an explicit `/v1/embeddings` call or an admin preload, or point it at an
always-on external daemon. Set `false` to allow on-demand embedding loads (the
documented opt-in exception).

The threshold default **0.95** is precision-favoring: the broker is multi-tenant
with no built-in eval loop, and a false positive serves a *wrong* answer. See
`docs/research/adr-0014-semantic-cache-and-cache-extensions.md` for the
false-positive-vs-threshold survey (GPTCache, Redis, LiteLLM, respan).

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_SEMANTIC_CACHE_ENABLED` | bool | `false` | Master switch for the semantic layer. |
| `BROKER_SEMANTIC_CACHE_MODEL` | str | — | **Required when enabled**: registry alias served via `/v1/embeddings`. |
| `BROKER_SEMANTIC_CACHE_THRESHOLD` | float | `0.95` | Cosine similarity in `[0,1]`; higher = fewer false positives, lower hit rate. |
| `BROKER_SEMANTIC_CACHE_TTL_SECONDS` | float | `300` | Operator TTL ceiling per bucket; `0` = no TTL. A per-request `x-broker-cache-ttl` may only shorten it. |
| `BROKER_SEMANTIC_CACHE_MAX_ENTRIES` | int | `128` | Per-bucket bound on stored `(embedding, response)` pairs (LRU); `>= 1`. |
| `BROKER_SEMANTIC_CACHE_MAX_BUCKETS` | int | `1024` | Global LRU bound on the **number** of buckets; `>= 1`. Keeps the index bounded even when a caller varies non-prompt params. |
| `BROKER_SEMANTIC_CACHE_REQUIRE_RESIDENT` | bool | `true` | Consult/populate only when the embedding model is already resident; `false` allows on-demand loading. |

## Admin runtime control (ADR 0012)

Privileged control plane (`GET /admin/models`, model unload/load, registry
hot-reload), **default off**. When enabled, a request is authorized by **either**
an ADR 0008 virtual key whose scope is `admin` **or** a value in the dedicated
`BROKER_ADMIN_API_KEYS` list — both intentionally separate from an ordinary
data-plane key, and both compared byte-wise (a malformed key is a clean `401`, not
a `500`). The admin surface stays locked even when data-plane auth is disabled.

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_ADMIN_ENABLED` | bool | `false` | Expose `/admin/*`. When off, the routes return a structured `admin_disabled` error (HTTP 400). |
| `BROKER_ADMIN_API_KEYS` | list[str] | `[]` | Admin keys (presented as `X-Admin-Key`, or `Authorization: Bearer`). When admin + auth are on, you must configure **either** this **or** a `BROKER_KEYS_PATH` holding an `admin`-scope virtual key; startup fails fast otherwise. |
| `BROKER_ADMIN_DRAIN_TIMEOUT_SECONDS` | float | `30` | How long `unload ?drain=true` / registry reload waits for in-flight requests to finish before giving up. |

## Web UI / playground (ADR 0012) — deferred

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_UI_ENABLED` | bool | `false` | Reserved for a future static `/ui`. **Deferred** — currently inert. |

## Observability

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `BROKER_METRICS_ENABLED` | bool | `true` | Expose `/metrics`. |
| `BROKER_TRACING_ENABLED` | bool | `false` | OpenTelemetry tracing (ADR 0012). Requires the optional `otel` extra or startup fails fast. Spans carry `gen_ai.*` metadata only — never prompt/response content. OTLP exporter configured via standard `OTEL_*` env vars. No-op + zero overhead when off. |
| `BROKER_TRACING_SERVICE_NAME` | str | `backend-agnostic-broker` | `service.name` resource attribute for spans (when the broker stands up its own provider). |
| `BROKER_TRACING_CAPTURE_CONTENT` | bool | `false` | **(ADR 0014)** Opt-in span content capture. When tracing is enabled AND this is `true`, the `inference` span carries truncated `gen_ai.input.messages` / `gen_ai.output.messages` (OTel GenAI `spanOnly` mode). **WARNING: prompts/responses may contain PII** and become visible to anyone with trace access — never enable without an informed decision. Off = byte-for-byte v0.5.0 (no content on any span). |
| `BROKER_TRACING_CONTENT_MAX_CHARS` | int | `2000` | Truncate each captured content attribute to this many characters (`0` = unlimited). |
| `BROKER_USAGE_LOG_ENABLED` | bool | `true` | Record per-request usage to the SQLite usage log (metadata + token counts only; see `/usage`). |
| `BROKER_USAGE_LOG_PATH` | str? | `<cache_dir>/usage.sqlite3` | Usage-log path; `:memory:` for ephemeral. Self-disables if unwritable. |

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
