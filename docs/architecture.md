# Architecture Overview

`backend-agnostic-broker` is an OpenAI-compatible HTTP broker that routes chat
requests to multiple local inference backends (Strata, Ollama, vLLM, llama.cpp,
and future backends) behind one API. Models load **only when a request selects
them**, idle models unload automatically, and process lifetime, GPU placement,
tensor/layer split, and caching are all configurable.

## Components

| Module | Responsibility |
|--------|----------------|
| `broker/app.py` | FastAPI app: OpenAI + ops routes, auth, request-size limit, request IDs, structured errors, streaming. |
| `broker/manager.py` | `ModelManager` / `ManagedModel`: lazy loading, per-model load lock, global + per-model concurrency, idle sweep, LRU eviction, **pool routing (sticky/least-busy/round-robin/cache-aware) with failover, retries + full-jitter backoff, and member cooldowns**, **non-loading health probe**, graceful shutdown, status aggregation. |
| `broker/state.py` | `StateStore`: async abstraction over routing/runtime state — sticky map, counters (+ sliding-window), cooldown records, generic bytes/JSON KV. `InProcessStateStore` (default) and optional `RedisStateStore` (multi-replica). See [StateStore](#statestore-shared-routingruntime-state-adr-0009). |
| `broker/cache_routing.py` | Cache-aware routing (ADR 0010): prompt normalization, chained character-block hashing, and the `CacheRouter` prefix→member index (on the `StateStore`) + affinity scoring inputs for `cache_aware`. |
| `broker/router.py` | `AutoRouter` for the `auto` alias: flag extraction, Stage-1 capability filter, data-driven overrides, heuristic + llama.cpp (GBNF-constrained) classifier backends, decision cache + fallback. |
| `broker/lifecycle.py` | Pure state machine (`unloaded → loading → ready → busy → unloading → failed → stopping`) with explicit legal transitions. |
| `broker/adapters/` | Backend adapter contract (`base.py`), shared HTTP adapter (`httpbase.py`), and one adapter per backend (`fake`, `ollama`, `vllm`, `llamacpp`, `strata`). |
| `broker/registry.py` | Declarative, pydantic-validated model registry (YAML/JSON). |
| `broker/security.py` | Trust boundaries: executable allow-list, model-path validation, request-flag rejection, no-shell argv construction, SSRF guard. |
| `broker/proc.py` | `ProcessSupervisor`: no-shell child-process lifecycle for broker-managed runtimes. |
| `broker/config.py` | Typed, fail-fast `Settings` (env prefix `BROKER_`). |
| `broker/auth.py` | API-key auth (Bearer / `X-API-Key`, constant-time compare). |
| `broker/errors.py` | Structured error hierarchy → OpenAI-style error envelopes. |
| `broker/metrics.py` | Prometheus metrics (`REQUESTS`, `REQUEST_LATENCY`, `INFLIGHT`, `LOADED_MODELS`). |

## Lifecycle state machine

States and the legal transitions enforced in `broker/lifecycle.py` (`_TRANSITIONS`):

```
unloaded  → loading, stopping
loading   → ready, failed, stopping
ready     → busy, unloading, stopping, failed
busy      → ready, unloading, stopping, failed
unloading → unloaded, failed, stopping
failed    → unloaded, loading, stopping
stopping  → unloaded, failed
```

Any illegal transition raises `LifecycleError` (an internal bug signal) instead
of silently corrupting state. The machine is pure (no I/O); the manager drives
transitions.

## Request flow (chat completion)

The data-plane pipeline composes every P1/P2 feature in one fixed order (the same
order for `/v1/chat/completions`, `/v1/completions`, `/v1/responses`, and — minus
auto/cascade — `/v1/embeddings`):

> **auth → key limits/budget (ADR 0008) → placement pinning (ADR 0011) → response
> cache lookup (ADR 0012) → auto/cascade (ADR 0011) → pool selection (ADR 0010
> `cache_aware`/sticky) with retries + cooldowns (ADR 0009) → inference → usage
> record + cache store.**

1. **Auth** — `app.py` middleware assigns a request ID and enforces the
   content-length limit; the route resolves the caller's **Principal** from the key
   store (`authed`: ADR 0008 virtual keys + legacy flat `BROKER_API_KEYS`). An
   unknown/invalid/revoked/expired/malformed key yields one generic 401 (a
   non-ASCII/over-long key is compared byte-wise, never a 500). With auth disabled
   the caller is an identity-less unlimited admin principal.
2. **Validate selector** — the body is parsed; `reject_request_flags` rejects
   flag-like/shell-metacharacter tokens in `model`; a fast `allowed_models`
   precheck 403s a directly-named, disallowed model before admission.
3. **Key limits / budget (ADR 0008)** — the `Governor` admits the request against
   the key's `rpm` (sliding window), `tpm` (pre-estimate + post-reconcile),
   `max_concurrency`, and daily/monthly token & cost budgets on the shared
   `StateStore`. Over-limit → `429 rate_limit_exceeded` (+ `Retry-After` +
   `x-ratelimit-*`); over-budget → `429 insufficient_quota`. A reservation is
   reconciled with real token usage after serving.
4. **Placement pinning (ADR 0011, fail-closed)** — the requirement
   (`x-broker-placement` ∪ `BROKER_DEFAULT_PLACEMENT`) is resolved up front; only
   models/pool members carrying **every** required label are eligible, else
   `403 placement_denied` (never spills to a non-compliant model).
5. **Response cache lookup (ADR 0012)** — if the cache is enabled and the request
   is cacheable (deterministic-by-default; embeddings always), a key over
   *(API-key/tenant + placement scope, resolved public model, normalized
   payload + sampling + tools)* is looked up; a hit returns immediately with
   `x-broker-cache: hit` and no inference. Bypass via `Cache-Control: no-cache`/
   `no-store` or `x-broker-cache: bypass`.
6. **Resolve + auto/cascade (ADR 0011)** — for `auto`, the `AutoRouter` picks a
   concrete public model or a two-hop **cascade** plan (serve cheap, escalate once
   on a quality signal). The resolved model — and each cascade hop — is re-checked
   against the key's `allowed_models` (fail-closed, so `auto` can **never** serve a
   disallowed model). `_members_for` raises `ModelNotFoundError` if unknown (**no
   silent fallback**).
7. **Pool selection + reliability** — `_select_member` picks a member per
   `route_strategy`: **`sticky`** (default) pins a session (`X-Session-Id`, else
   OpenAI `user`, else client IP) to one member; `least_busy`/`round_robin` pick
   per request; **`cache_aware`** (ADR 0010) scores members by prompt-prefix
   affinity minus relative load (`broker/cache_routing.py`), falling back to
   `least_busy`. Transparent **retries** with full-jitter backoff and per-member
   **cooldowns** (ADR 0009) stay **within the requested model's pool** (never a
   different model). The member's in-flight slot is reserved under a route lock.
8. **Acquire the member**:
   - `_maybe_evict(alias)` unloads the LRU idle model if `max_loaded_models` is
     reached (raises `ConcurrencyLimitError` if nothing can be evicted).
   - `ensure_ready()` starts + readies the model **exactly once** under an
     `asyncio.Lock` (`load_lock`). A previously `failed` model is reset to
     `unloaded` and retried.
   - On a backend error (`backend_unavailable`/`model_load_error`/`timeout`), the
     member is marked unhealthy and the request **fails over** to another pool
     member (before the first byte, for streams).
9. **Concurrency** — a global semaphore caps total in-flight requests; a
   per-model semaphore caps per-model concurrency. With
   `load_concurrency_policy=reject`, a saturated per-model semaphore returns
   HTTP 429 instead of queueing.
10. **Serve + record** — the model transitions `ready → busy`; the adapter runs
    `chat` (or `stream_chat` for SSE) under a per-request timeout; `inflight` is
    tracked and the model returns to `ready` when drained. The response `model` is
    relabeled to the public name requested. On success the usage log records the
    request and the response cache **stores** the result (keyed as in step 5); a
    cascade is stored under its final served model, with `x-broker-route` showing
    the path. Streaming responses are never cached.
11. **Idle unload** — a background sweep (`_sweep_once`, every
    `idle_sweep_interval_seconds`) unloads any `ready` model idle beyond its
    timeout. A separate **health probe** (`_health_probe_once`, every
    `health_probe_interval_seconds`) liveness-checks active models **without
    loading any** and marks a `ready` model `failed` if its backend has gone away.

## Lazy loading: broker-managed vs native

- **Broker-managed** (vLLM, llama.cpp, Strata): these runtimes load a model at
  **process start**, so the broker starts the child process on the first
  selecting request (`adapter.start()` + `wait_until_ready`) and terminates it
  after the idle timeout (`adapter.unload()`/`stop()`). Child processes are
  launched via `ProcessSupervisor` with an argv **list** (never a shell string).
- **Native** (Ollama): Ollama manages its own model lifetime; the adapter uses
  its HTTP API and `keep_alive` for load/unload, so broker `start` is a
  readiness no-op.

See `docs/backends/` for per-backend detail and `docs/backend-adapter.md` for
the contract.

## Security boundaries

Every externally-influenced value crosses a boundary in `broker/security.py`:
executables must be allow-listed and absolute; model paths must resolve inside
allow-listed roots; request fields that could reach a child process are scrubbed
of flags/metacharacters; argv is always a list built from a whitelisted
template; upstream URLs pass an SSRF allow-list that also blocks
link-local/metadata addresses. See `docs/security-model.md` and
`docs/threat-model.md`.

## Observability

- `GET /status` — per-model lifecycle snapshot (state, inflight, pid, idle
  seconds, last error, live `healthy`/`last_health`, and `cooldown` when enabled)
  plus adapter status (endpoint, GPU allocation), the `pools` map,
  `route_strategy` / `sticky_sessions`, `state_backend` (`in_process`|`redis`),
  `retry_max_attempts`, `cooldowns_enabled`, a `reliability` block (ejection cap,
  cooldown growth, half-open, Retry-After, Redis sticky bound — ADR 0017),
  `tracing_enabled`, and a `response_cache` stats block (ADR 0012).
- `GET /healthz/backends` — live, non-loading per-backend liveness (`ok`/`degraded`), with per-member `cooldown`.
- `GET /api/ps` — Ollama residency passthrough (what is actually in memory).
- `GET /metrics` — Prometheus exposition (when `metrics_enabled`).
- **OpenTelemetry tracing** (ADR 0012, opt-in `tracing_enabled` + the `otel`
  extra): `broker/tracing.py` wraps the request path in `gen_ai.*` spans
  (`auto_route`→`load_model`→`inference`→`cache_lookup`). Content is never
  captured; the OTLP exporter uses standard `OTEL_*` env vars. Disabled = a no-op
  null tracer with zero overhead; enabled-but-not-installed = fail-fast
  `ConfigError`. Prometheus is unchanged.
- Structured JSON logs (`log_format=json`) or text. Admin actions are logged by
  the `broker.admin` logger (ADR 0012).

See [routing-and-health.md](routing-and-health.md) for pools, routing, failover,
and health details.

## Response cache (ADR 0012)

`broker/cache.py` (`ResponseCache`) is a thin policy layer over the StateStore
`kv_*` API. Off by default. When enabled, the API layer wraps each non-streaming
inference endpoint in a cache lookup: resolve `auto` → build the key `sha256(key/
tenant scope, resolved public model, endpoint, normalized body)` → return the
stored result on a hit, else serve and store. The scope-first key guarantees
per-key isolation; only deterministic (or embedding) requests are cached by
default; errors and streams are never stored. Backed by the same StateStore as
routing state, so it is in-process by default or shared via Redis.

## Admin runtime control (ADR 0012)

Off by default. The `ModelManager` exposes `admin_list_models`, `admin_unload`
(refuse/drain on in-flight), `admin_load` (explicit preload — the only
non-request load path), and `admin_reload_registry` (transactional: validate the
whole candidate registry and all adapters before mutating; drain changed/removed
members; keep unchanged instances live; invalid → no change). The routes sit
behind `require_admin` (a dedicated admin-key gate, the swap point for ADR 0008's
admin scope). A member being drained sets `draining=True`, which member selection
skips — the two-phase connection-drain pattern.

## StateStore: shared routing/runtime state (ADR 0009)

`broker/state.py` is the single async abstraction over the small pieces of state
the routing layer (and future subsystems) keep. In one process it is in-memory
dicts; across replicas it is Redis. The `ModelManager` owns one `StateStore`
(`self.state`), created by `make_state_store(settings)`:

- **`InProcessStateStore`** — the default. Zero new dependencies, exact v0.4.0
  semantics, sticky map LRU-bounded by `BROKER_ROUTE_STICKY_MAX_ENTRIES`.
- **`RedisStateStore`** — selected when `BROKER_REDIS_URL` is set. Namespaces keys
  with `BROKER_REDIS_PREFIX`, uses one shared `redis.asyncio` client (closed at
  shutdown). `redis` is an optional extra, import-guarded → `ConfigError` if
  enabled-but-missing.

### API (all `async`)

| Group | Methods | Used by |
|-------|---------|---------|
| Sticky map | `sticky_get(pool, session)`, `sticky_set(pool, session, member)`, `sticky_len()` | manager sticky routing (ADR 0003/0009) |
| Counters | `incr(key, amount, ttl)`, `get_counter(key)`, `window_incr(key, window_s, …)`, `window_count(key, window_s, …)` | **ADR 0008** per-key rate limits (rpm/tpm sliding window) |
| Cooldowns | `record_failure(key, *, threshold, cooldown_seconds, multiplier, max_cooldown_seconds)`, `record_success(key, decay_seconds)`, `get_cooldown(key)` → `CooldownRecord`, `try_half_open(key, ttl_seconds)` | manager member cooldowns (ADR 0009) + ejection growth / single-trial half-open (ADR 0017) |
| Generic KV | `kv_get/kv_set` (bytes + TTL), `kv_get_json/kv_set_json`, `kv_delete` | **ADR 0010** prefix→member index, **ADR 0012** response cache |
| Sorted-set LRU | `zlru_touch(index, member, score, ttl)`, `zlru_trim(index, max_entries)` → evicted members, `zlru_size(index)` | **ADR 0018** cross-replica prefix-index size bound (shared under Redis) |
| Lifecycle | `close()` | shutdown |

`CooldownRecord` carries `failures`, `until`, `now`, and `ejections` (ADR 0017),
with derived `in_cooldown`, `recovering`, `seconds_remaining`, and `to_dict()`
(what `/status` and `/healthz/backends` render). The exponential ejection count
and last-ejection timestamp are stored separately from the `(failures, until)`
record so repeated failures within one open window are a single ejection episode.
ADR 0017 uses `try_half_open` to gate single-trial probing, and `record_failure`'s
`multiplier`/`max_cooldown_seconds` to grow the window on repeated ejections
(decaying via `record_success(decay_seconds=…)`). Redis sticky additionally keeps
an optional sorted-set LRU index (`BROKER_REDIS_STICKY_MAX_ENTRIES`). The sliding-
window counter follows the Redis sorted-set "sliding window log" pattern; TTL keys
back sticky/cooldown/cache expiry. The API is intentionally broader than the
manager uses today — it is the shared-state platform the P1/P2 ADRs build on, so
0008/0010/0012/0017 add consumers, not new stores.
