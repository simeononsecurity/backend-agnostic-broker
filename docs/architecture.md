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
| `broker/proc.py` | `ProcessSupervisor`: no-shell child-process lifecycle for broker-managed runtimes; records descendants while a runtime runs and `stop()` returns a `TeardownResult` for the whole tree (ADR 0020 / 0024). |
| `broker/teardown.py` | Deadline-driven, descendant-aware runtime teardown (ADR 0024): identity-gated `terminate_tree`, `/proc`+`ps` descendant enumeration, Linux child-subreaper (`prctl`), and the `TeardownResult` + teardown metrics. |
| `broker/resources.py` | Vendor-neutral `ResourceTelemetry` providers (ADR 0020): `off`/`nvidia`/`amd`/`host`/`auto`, argv-only + allow-listed + defensive parsing; `resources_probe.py` CLI. Feeds measured release and memory-aware admission (ADR 0022). |
| `broker/admission.py` | `AdmissionQueue` (ADR 0023): bounded/timed/fair admission-queue scheduler — ordering (FIFO-per-key + round-robin + priority/aging), depth bounds, wake selection, and `/status` stats. The manager owns the overload probes + semaphore acquisition. |
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
unloading → unloaded, failed, stopping, release_stuck
failed    → unloaded, loading, stopping
stopping  → unloaded, failed
unloaded  → loading, stopping, release_stuck
release_stuck → unloaded, loading, stopping
```

Any illegal transition raises `LifecycleError` (an internal bug signal) instead
of silently corrupting state. The machine is pure (no I/O); the manager drives
transitions. `release_stuck` (ADR 0020) marks a model whose device memory did not
come back after eviction; `/status` carries the stuck PID/device/MiB.

## Request flow (chat completion)

The data-plane pipeline composes every P1/P2 feature in one fixed order (the same
order for `/v1/chat/completions`, `/v1/completions`, `/v1/responses`, and — minus
auto/cascade — `/v1/embeddings` and `/v1/rerank`):

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
5. **Response cache lookup (ADR 0012/0014)** — if the cache is enabled and the
   request is cacheable (deterministic-by-default; embeddings and rerank always), a
   key over *(API-key/tenant + placement + namespace scope, resolved public model,
   normalized payload + sampling + tools)* is looked up; an exact hit returns
   immediately with `x-broker-cache: hit` and no inference. On an exact miss, if
   the **semantic cache** (ADR 0014) is enabled and its embedding model is already
   **resident**, the last user turn's text is embedded and matched (cosine ≥
   threshold) within the same strict non-prompt bucket → `x-broker-cache:
   semantic-hit` + `x-broker-cache-similarity`. **Streaming** hits are replayed as
   well-formed SSE. Bypass via `Cache-Control: no-cache`/`no-store`/`max-age=0` or
   `x-broker-cache: bypass`; per-request `x-broker-cache-ttl` (finite; only shortens
   the operator TTL; `0` = do not store) / `x-broker-cache-namespace` tune
   storage/scope.
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
8. **Acquire the member** — `_maybe_evict(alias)` makes room, in order (ADR 0020 /
   0022):
   - **Memory-aware admission** (ADR 0022): a model declaring `memory_mib` is
     admitted only if it fits its device budget — measured free memory (minus
     `BROKER_GPU_MEMORY_HEADROOM_MIB`) when telemetry is on, else the declared
     `BROKER_GPU_MEMORY_BUDGET_MIB`. It evicts LRU idle models **on the same
     device(s)** to fit, else raises a typed `CapacityUnavailableError` (429,
     `reason` + `holder`). Unsized models skip this step.
   - **Measured release** (ADR 0020): every eviction is followed — when telemetry
     is on — by a wait until the evicted process group is gone **and** the device
     stops reporting its PID, bounded by `BROKER_RELEASE_TIMEOUT_SECONDS`. On
     timeout the victim enters `release_stuck` and the load fails 503 rather than
     OOMing. The broker never resets a GPU or kills a foreign process.
   - **Count limit** (v0.6.0): the LRU idle model is unloaded if
     `max_loaded_models` is reached (raises `ConcurrencyLimitError` if nothing can
     be evicted). An already-resident member holds its own slot and skips this step.
   - **Device-lease admission** (ADR 0026, opt-in `BROKER_DEVICE_LEASES=on`): the
     model's devices are leased **before spawn**, inside the same atomic admission
     step. A `gpu.exclusive` model takes an `exclusive` lease; others take a
     `shared` lease. An incompatible idle broker holder is evicted under the policy
     above; a busy broker holder or any **external** holder fails the load with
     `device_busy` (503 + `Retry-After`), and an external holder is never signalled.
   - **Port pre-check + ledger record, then spawn** (ADR 0021): before starting a
     broker-managed runtime, `_port_precheck` fails fast (naming the port) if the
     target port is already bound (`BROKER_RUNTIME_PORT_PRECHECK`, default on); after
     a successful spawn, `ensure_ready` writes a durable ledger record
     (PID + start-time identity) for restart reconciliation. A model that is
     **adopted** (reused from a still-healthy orphan in `adopt` mode) is marked
     `ready` **without spawning** and therefore counts toward both the
     `max_loaded_models` limit and the ADR 0022 memory accounting, and is evictable
     like any other resident — see the ADR 0021 section below.
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

   **Admission queue (ADR 0023, opt-in)** — when `BROKER_QUEUE_ENABLED` is on, the
   whole admit step (memory/count eviction, ready, and the two semaphores) is
   wrapped by a **bounded, timed, fair** queue (`broker/admission.py`). A request
   that cannot be admitted now — concurrency saturation, queueable capacity
   (ADR 0022), a model mid load/release, or a measured overload signal (device
   memory / host load / backend backpressure) — waits in the queue instead of
   blocking a semaphore, acquiring the slots only when it is its turn and no
   overload is active. FIFO within an API key, round-robin across keys, optional
   per-key priority with aging. Full → 429 `queue_full`; wait-timeout → 503
   `queue_timeout` (both + `Retry-After`). It composes with eviction/release,
   retries/failover (a queue verdict is terminal), the cache (hits bypass it), the
   auto-router (resolved before queueing — never switches model), and streaming
   (the wait is before the first byte). `exceeds_device_capacity` fails fast. OFF
   == v0.6.0.
10. **Serve + record** — the model transitions `ready → busy`; the adapter runs
    `chat` (or `stream_chat` for SSE) under a per-request timeout; `inflight` is
    tracked and the model returns to `ready` when drained. The response `model` is
    relabeled to the public name requested. On success the usage log records the
    request and the response cache **stores** the result (keyed as in step 5); a
    cascade is stored under its final served model, with `x-broker-route` showing
    the path. **Only non-streaming** responses populate the cache; a streaming
    request reads (and can replay a stored result) but never writes (ADR 0014).
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

## Runtime identity & restart reconciliation (ADR 0021)

Broker-managed runtimes are spawned in their own session/process-group, so they
**outlive an unclean broker death** (SIGKILL / OOM / crash / container restart),
holding VRAM and their port with no in-memory record left. To recover safely the
broker records every runtime it spawns in a **host-local ledger**
(`<BROKER_CACHE_DIR>/runtime-ledger.json`, `0600`, atomic + `flock`), keyed by a
durable **identity = PID + process start time** (`broker/runtime_identity.py`)
plus the process-group id and an argv hash. A bare PID is never trusted — the
kernel recycles PID numbers — so a record whose re-read start time does not match
is treated as stale and never signalled.

On startup (`broker/reconcile.py`, from the lifespan, before the sweeper/health
loops, never loading a model) the broker reconciles the ledger per
`BROKER_RUNTIME_RECONCILE`: `off`, `report` (default — list orphans in `/status`),
`adopt` (reuse a still-healthy orphan: mark READY without spawning), or `stop`
(`SIGTERM`→grace→`SIGKILL` the recorded group). A clean shutdown is bounded by
`BROKER_SHUTDOWN_BUDGET_SECONDS` and tears down every runtime **and its
descendants** (ADR 0024, below), then clears the ledger. The scope is per host
(local processes), so the ledger is never shared via Redis. See
`docs/guides/restart-and-recovery.md`.

**Bounded, descendant-aware shutdown (ADR 0024).** `ModelManager.shutdown()` runs
against one total budget (`BROKER_SHUTDOWN_BUDGET_SECONDS`, default 45 s) whose
deadline is stamped at the **first** stop signal, so uvicorn's graceful phase and the
runtime teardown draw from the same budget. On that first signal the broker immediately
cancels in-progress loads and stops admitting new ones (`begin_preemption`): a client
waiting on a cold load fails fast with a typed **503 `shutting_down`** (with `Retry-After`)
instead of pinning shutdown open. uvicorn's graceful HTTP phase is capped at
`timeout_graceful_shutdown = min(10 s, budget/3)`, leaving the rest for runtime SIGTERM
grace (`min(BROKER_SHUTDOWN_TIMEOUT_SECONDS, remaining budget)`). The kill set is the
runtime's process group **plus every recorded descendant** — on Linux the broker is a
child subreaper (`prctl(PR_SET_CHILD_SUBREAPER)`) and walks `/proc`; elsewhere a periodic
argv-only `ps` snapshot records descendants while the runtime lives — so an escaped
`setsid` GPU worker is still torn down. After the tree is down, descendants that
re-parented to the broker and became zombie children are **reaped** (`waitpid(WNOHANG)`,
only the specific kill-set PIDs whose parent is the broker — never `waitpid(-1)`), inline
in `terminate_tree` and periodically in the idle sweeper, so a long-running broker that
churns runtimes does not accumulate zombies. Every stop is identity-gated (PID + start
time, never a process the broker didn't start), produces a `TeardownResult` (signalled /
exited / survivors / reaped / duration) that is logged, counted in
`broker_runtime_teardown_total{outcome}` + a duration histogram, and surfaced via
`survived_kill` / `release_stuck` in `/status`. A second `SIGTERM`/`SIGINT` during
shutdown escalates straight to `SIGKILL` of every runtime tree.

**Ownership lock.** Because the ledger is host-local, two brokers that accidentally
share a `BROKER_CACHE_DIR` could otherwise stop or adopt each other's live
runtimes. Each broker therefore takes a broker-lifetime exclusive `flock` on
`runtime-ledger.owner.lock`: `adopt`/`stop` **refuse to start** (a `ConfigError`
naming the cache dir + owner PID) while a live sibling holds it, and `report`/`off`
start with the ledger disabled (`reconciliation.disabled_reason` + `ledger_owned`
in `/status`), preserving v0.6.0 behavior. A stale lock (owner dead) is taken over.
As defense in depth every record carries its owning broker's identity, and
reconciliation only ever acts on records whose owner is dead — a live sibling's
record is listed as `owned_by_live_broker` and never signalled, adopted, or
dropped.

**Adopted runtimes are broker-owned.** A model adopted in `adopt` mode transitions
`unloaded → ready` **without a spawn**, so it is a resident exactly like a spawned
one: it counts toward `max_loaded_models` (the `loaded_count`), it contributes its
declared `memory_mib` to the ADR 0022 memory accounting (and is a valid LRU
eviction victim), and — when telemetry is on — its device memory is attributed by
PID in the `resources` block like any other holder. Its provenance is visible in
`/status` as `models[].process.adopted = true` and in the last startup's
`reconciliation.last` summary (`adopted` list, alongside `stopped`, `orphaned`,
`stale`, and `owned_by_live_broker`). A model reported (not adopted) by a sibling
broker in `report` mode is **not** counted by that broker at all.

## Device leases (ADR 0026)

Opt-in (`BROKER_DEVICE_LEASES=on`). The broker keeps one lease file per device it
uses under `BROKER_DEVICE_LEASE_DIR` (atomic + flock, world-readable), reusing the
runtime-identity and owner-lock machinery of ADR 0021. Leases give co-tenants a
readable device-occupancy signal, enforce `gpu.exclusive` admission, and let a
forced stop be cleaned up from `ExecStopPost=`.

The lease lifecycle follows a single ordering, **acquire → spawn → … →
shutdown → teardown → release → lease-drop**:

- **Acquire** happens inside the atomic admission step, **before** spawn, so a
  device is never double-booked (see the request flow above); the runtime
  identity is stamped onto the lease once the load succeeds.
- **Release** is driven by one decision — "is the runtime gone?" — computed by
  `ManagedModel.lease_released()` (today the measured-release signal
  `adapter.last_release_ok()`; ADR 0024's richer bounded-teardown result is wired
  in here by the integration card). On teardown the broker **first** confirms the
  process group is gone and memory released, **then** drops the lease and fires
  the release hook. If the runtime survives, the lease is marked `stuck` and the
  sweeper clears it once the survivors exit.

Stale leases (broker dead **and** runtime gone) are removed at startup (before
serving), by the periodic sweeper, and by the one-shot `python -m broker cleanup`
(`ExecStopPost=`). An orphan (broker dead, runtime alive) follows
`BROKER_RUNTIME_RECONCILE`; an **external** holder is never signalled, evicted, or
removed. `/status.leases` shows the current holders; Prometheus exposes
`broker_device_leases{state}`, `broker_device_lease_stale_cleanups_total`, and
`broker_device_lease_hook_failures_total{phase}`. See
[device-leases.md](device-leases.md).

## Security boundaries

Every externally-influenced value crosses a boundary in `broker/security.py`:
executables must be allow-listed and absolute; model paths must resolve inside
allow-listed roots; request fields that could reach a child process are scrubbed
of flags/metacharacters; argv is always a list built from a whitelisted
template; upstream URLs pass an SSRF allow-list that also blocks
link-local/metadata addresses. See `docs/security-model.md` and
`docs/threat-model.md`.

## Observability

- `GET /status` — a single response composing the whole runtime view. Per-model:
  the lifecycle snapshot (state, inflight, idle seconds, last error, live
  `healthy`/`last_health`), a `process` object `{pid, start_time, adopted}`
  (ADR 0021), `cooldown` when enabled, and the adapter status (endpoint, GPU
  allocation). Top-level: `loaded_count`/`max_loaded_models`, the `pools` map,
  `route_strategy` / `sticky_sessions`, `state_backend` (`in_process`|`redis`),
  `retry_max_attempts`, `cooldowns_enabled`, a `reliability` block (ejection cap,
  cooldown growth, half-open, Retry-After, Redis sticky bound — ADR 0017),
  `tracing_enabled`, a `response_cache` stats block (ADR 0012), a `resources`
  block (ADR 0020/0022: measured devices + per-PID holders, declared admission
  budgets + recent decisions, `release_stuck`, and Ollama memory labeled
  `reported`), a `reconciliation` block (ADR 0021: mode, `ledger_owned`,
  `disabled_reason`, and the last startup's adopted/stopped/orphaned/stale/
  `owned_by_live_broker` summary), and a `queue` block (ADR 0023: depth, per-key
  waiters, triggers, and served/shed counts).
- `GET /healthz/backends` — live, non-loading per-backend liveness (`ok`/`degraded`), with per-member `cooldown`, and the same `resources` block as `/status`.
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

## Semantic cache + cache extensions (ADR 0014)

`broker/cache.py` (`SemanticCache`) is a second, opt-in layer the API consults
**after an exact miss** for chat/completions/responses. It partitions stored
responses into **buckets** keyed by every *non-prompt* field (scope, resolved
model, endpoint, sampling/tools/`response_format`/..., namespace, **the system
prompt, and the whole conversation history**), so only the **last user turn's
text** is fuzzy — a semantic hit can never cross a scope, model, param, system
prompt, or history boundary. The bucket count is bounded globally
(`BROKER_SEMANTIC_CACHE_MAX_BUCKETS`, LRU) alongside the per-bucket entry bound.
Within a bucket it embeds that last-turn text via the broker's own
`/v1/embeddings` path and compares to each stored embedding by **pure-Python
cosine** (no numpy), returning the best match ≥ threshold (default 0.95). To honor
lazy loading, the API embeds/consults **only when the embedding model is already
resident** (`manager.is_resident`), so a chat request never silently loads an
extra model (`BROKER_SEMANTIC_CACHE_REQUIRE_RESIDENT=false` opts out). Only
non-streaming responses populate either cache, and only under the **served** model
(a cascade escalation to a different model is not stored); a **streaming** hit is
replayed as a well-formed SSE stream (`openai_compat.chat_result_to_sse_chunks`
fed through the existing text/Responses translators). Per-request
`x-broker-cache-ttl` (finite, only shortens the operator TTL; `0` = do not store)
and `x-broker-cache-namespace` tune storage and scope; `max-age=0` is a bypass;
`s-maxage`/request `max-age>0` freshness are rejected (no stored-age/revalidation).
When `BROKER_TRACING_CAPTURE_CONTENT` is set, the `inference` span additionally
carries truncated `gen_ai.input.messages`/`gen_ai.output.messages` (OTel GenAI
`spanOnly`, opt-in; off by default).

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
| GCRA | `gcra(key, *, emission_interval, burst, cost=1.0)` → `GCRAResult` (`allowed`, `retry_after`, `remaining`, `reset_after`) | **ADR 0016** atomic GCRA/token-bucket rpm (`rate_limit_algo=gcra`; in-process + single-round-trip Redis Lua) |
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
