# Routing, Load Balancing & Health

This broker can spread traffic for one logical model across **multiple backend
hosts** (a *pool*), fail over when a host is down, and liveness-probe backends
**without ever loading a model**.

## Pools: one model name, many hosts

Give several registry entries the **same `pool` name**. Clients then call that
pool name as the `model`, and the broker routes each request to one member:

```yaml
# registry.yaml
models:
  - alias: chat-node-1          # unique member id
    backend: ollama
    runtime_model_id: llama3.1:8b
    pool: chat                  # <- public model name clients request
    base_url: ${OLLAMA_URL_1}   # host A (keep real IPs in .env)
    readiness: { kind: http, path: /api/tags, expect_status: 200 }
  - alias: chat-node-2
    backend: ollama
    runtime_model_id: llama3.1:8b
    pool: chat                  # <- same pool
    base_url: ${OLLAMA_URL_2}   # host B
    readiness: { kind: http, path: /api/tags, expect_status: 200 }
```

- Clients call `{"model": "chat", ...}`; `GET /v1/models` lists `chat` once with
  `pool_size: 2` and `pool_members: [chat-node-1, chat-node-2]`.
- `alias` stays a **unique member id**; the **public name** is the pool (or the
  alias when no pool is set). A name used by two members must have `pool` set on
  all of them, so a standalone alias can never silently collide with a pool
  (enforced at load — fail fast).
- Members can be **different hosts or even different backends**, as long as
  they're interchangeable for the caller. A capability (`streaming`/`tools`/
  `vision`) is advertised for the pool only if **every** member supports it;
  `context_limit`/`output_limit` are reported as the **minimum** across members.
- An entry with **no `pool`** behaves exactly as before — a standalone model
  addressed by its alias.

## How requests are routed

`BROKER_ROUTE_STRATEGY`:

- **`sticky`** (default) — **pin each session to one member** and keep using it;
  only switch members if that member is unhealthy/unavailable. This keeps a
  conversation on the same backend (warm cache / KV locality) instead of
  re-balancing every request. A session is identified, in order:
  1. the **`X-Session-Id`** request header, if present;
  2. else the OpenAI **`user`** field in the request body;
  3. else the **client IP**.

  A *new* session is assigned the least-busy member (load + round-robin), so
  distinct sessions still spread across the pool — but each individual session
  then sticks 1:1. Pairings are remembered in an LRU map bounded by
  `BROKER_ROUTE_STICKY_MAX_ENTRIES` (default 10000). Visible in `GET /status` as
  `route_strategy` and `sticky_sessions`.
- **`least_busy`** — route *every* request to the member with the fewest
  in-flight requests (tie-break prefers an already-loaded member, then
  round-robin). The in-flight slot is reserved at selection time, so N concurrent
  callers spread across hosts.
- **`round_robin`** — rotate members every request regardless of load.

Each member keeps its **own** independent lifecycle, load lock, per-model
concurrency, and idle timer. `BROKER_MAX_LOADED_MODELS` and
`BROKER_PER_MODEL_MAX_CONCURRENCY` still apply per member.

> **Sending a session id.** Any client can set `X-Session-Id: <conversation-id>`
> to control stickiness explicitly. OpenAI SDKs that send `user` get stickiness
> per user for free. With neither, callers are stuck per source IP.

### Failover

If a selected member's backend is unavailable (`backend_unavailable`,
`model_load_error`, or `timeout_error`), the request is **transparently retried
on another member** of the pool; the failed member is marked unhealthy and
excluded until it recovers. Under `sticky`, the session is **re-pinned** to the
healthy member it failed over to, so it does not keep retrying the dead one.
Non-streaming requests fail over on any attempt; streaming requests fail over
**before the first byte** is sent (once bytes are flowing a mid-stream failure
propagates, since the response has already begun). If every member is down you
get a single `backend_unavailable` pointing you at `GET /healthz/backends`.

> There is **no silent fallback to a different model** — failover only ever
> retries *equivalent members of the same pool*.

## Health & liveness (never loads a model)

Three surfaces, all non-loading:

### `GET /healthz/backends`
Live, on-demand liveness of every backend member:

```json
{
  "status": "ok",                        // or "degraded" if any member is unhealthy
  "backends": [
    {"alias": "chat-node-1", "model": "chat", "backend": "ollama",
     "state": "ready", "status": "ok", "detail": "tags 200", "inflight": 1},
    {"alias": "qwen-llamacpp", "model": "qwen-llamacpp", "backend": "llamacpp",
     "state": "unloaded", "status": "not_loaded", "detail": "model not loaded"}
  ]
}
```

- **Ollama** (external daemon) is probed via `GET /api/tags` — reports daemon
  liveness even when no model is resident.
- **Broker-managed** runtimes (vLLM/llama.cpp/Strata) are probed only while their
  process is **already running**; when not loaded they report `not_loaded`
  (**not** a failure, and never a spawn).

### `GET /status`
Per-member lifecycle + a cached `healthy` flag and `last_health` from the
background probe, plus the `pools` map. Cheap (no network per call).

### `GET /api/ps`
Passthrough of Ollama's `/api/ps` across all configured Ollama daemons
(deduplicated by `base_url`) — shows what is **actually resident in VRAM/RAM**,
distinct from the broker's lifecycle state. Non-loading.

```json
{"daemons": [{"base_url": "http://hostA:11434", "models": [{"name": "llama3.1:8b", ...}]}]}
```

### Background probe

When `BROKER_HEALTH_PROBE_ENABLED=true` (default), a loop every
`BROKER_HEALTH_PROBE_INTERVAL_SECONDS` (default 30) liveness-probes active models
with the non-loading calls above. A **`ready` member whose backend has gone
away is marked `failed`**, so the next request reloads it (managed) or reroutes
to a healthy pool member — instead of the caller hitting a dead endpoint
mid-flight. Probes use `BROKER_HEALTH_PROBE_TIMEOUT_SECONDS` (default 5s) and
never touch a member that is mid-request.

> **Why there's no "warm keep-alive that avoids loading":** for a *model*,
> keeping it alive *is* keeping it resident — the concept only exists once
> loaded. Ollama's `keep_alive` governs how long a loaded model stays resident
> (the broker uses `keep_alive: 0` to unload on idle). The probes above are
> *daemon/process* liveness, which is what you can check without a load.

## Settings reference

| Env var | Default | Meaning |
|---|---|---|
| `BROKER_ROUTE_STRATEGY` | `sticky` | `sticky`, `least_busy`, or `round_robin` |
| `BROKER_ROUTE_STICKY_MAX_ENTRIES` | `10000` | max remembered session→member pairings (LRU) |
| `BROKER_HEALTH_PROBE_ENABLED` | `true` | enable the background liveness probe |
| `BROKER_HEALTH_PROBE_INTERVAL_SECONDS` | `30` | seconds between probe sweeps |
| `BROKER_HEALTH_PROBE_TIMEOUT_SECONDS` | `5` | per-probe timeout |

Registry: add `pool: <name>` to each member entry. See
[model-registry.md](model-registry.md) and [registry.example.yaml](../registry.example.yaml).
