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
| `broker/manager.py` | `ModelManager` / `ManagedModel`: lazy loading, per-model load lock, global + per-model concurrency, idle sweep, LRU eviction, **pool routing (least-busy/round-robin) with failover**, **non-loading health probe**, graceful shutdown, status aggregation. |
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

1. **Auth + limits** — `app.py` middleware assigns a request ID and enforces the
   content-length limit; the route calls `check_auth`.
2. **Validate selector** — the request body is parsed; `reject_request_flags`
   rejects flag-like/shell-metacharacter tokens in `model`.
3. **Resolve + route** — the `model` names a public model (a standalone alias or
   a **pool** of members). `_members_for` raises `ModelNotFoundError` if unknown
   (**no silent fallback**). `_select_member` picks a member — least-busy
   (round-robin tie-break) by default, or round-robin — and reserves its
   in-flight slot under a route lock so concurrent callers spread across members.
4. **Acquire the member**:
   - `_maybe_evict(alias)` unloads the LRU idle model if `max_loaded_models` is
     reached (raises `ConcurrencyLimitError` if nothing can be evicted).
   - `ensure_ready()` starts + readies the model **exactly once** under an
     `asyncio.Lock` (`load_lock`). A previously `failed` model is reset to
     `unloaded` and retried.
   - On a backend error (`backend_unavailable`/`model_load_error`/`timeout`), the
     member is marked unhealthy and the request **fails over** to another pool
     member (before the first byte, for streams).
5. **Concurrency** — a global semaphore caps total in-flight requests; a
   per-model semaphore caps per-model concurrency. With
   `load_concurrency_policy=reject`, a saturated per-model semaphore returns
   HTTP 429 instead of queueing.
6. **Serve** — the model transitions `ready → busy`; the adapter runs `chat` (or
   `stream_chat` for SSE) under a per-request timeout; `inflight` is tracked and
   the model returns to `ready` when drained. The response `model` is relabeled
   to the public name requested.
7. **Idle unload** — a background sweep (`_sweep_once`, every
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
  seconds, last error, live `healthy`/`last_health`) plus adapter status
  (endpoint, GPU allocation) and the `pools` map.
- `GET /healthz/backends` — live, non-loading per-backend liveness (`ok`/`degraded`).
- `GET /api/ps` — Ollama residency passthrough (what is actually in memory).
- `GET /metrics` — Prometheus exposition (when `metrics_enabled`).
- Structured JSON logs (`log_format=json`) or text.

See [routing-and-health.md](routing-and-health.md) for pools, routing, failover,
and health details.
