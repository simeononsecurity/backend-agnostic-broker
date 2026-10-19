# ADR 0003: Load-balancing pools, sticky routing, and non-loading health

- Status: Accepted
- Date: 2026-10-17
- Supersedes: none
- Related: ADR 0002 (architecture review), `docs/routing-and-health.md`

## Context

A single public model name often needs to be served by **several backend hosts**
(e.g. two Ollama daemons) for capacity and failover. The original design mapped
one public name to exactly one model. We also needed to detect a backend that has
gone away **without loading a model** (loading to "check health" is wasteful and
violates the lazy-load guarantee).

## Decision

1. **Pools.** Registry entries may share a `pool` name; they become one logical
   model addressed by the pool name. `alias` stays a unique member id. A name used
   by multiple members must set `pool` on all of them (fail-fast at load), so a
   standalone alias can never silently collide with a pool. `/v1/models` collapses
   a pool to one entry (capabilities ANDed, limits min-ed across members).

2. **Routing strategy is configurable** (`BROKER_ROUTE_STRATEGY`), default
   **`sticky`**: a session (X-Session-Id header → OpenAI `user` → client IP) is
   pinned 1:1 to a member and reused for warm-cache / KV locality, switching only
   on failure; new sessions get the least-busy member so distinct sessions still
   spread. `least_busy` and `round_robin` remain available. The in-flight slot is
   reserved at selection under a route lock. Sticky pairings are LRU-bounded.

3. **Failover, not fallback.** On a backend error the request transparently
   retries **another member of the same pool** (before the first byte for
   streams), and sticky re-pins the session. There is still **no fallback to a
   different model**.

4. **Non-loading health.** A background probe and `GET /healthz/backends`
   liveness-check active members without loading: Ollama via `GET /api/tags`;
   broker-managed runtimes only while their process is running (unloaded =
   `not_loaded`, never a spawn). A `ready` member whose backend vanished is marked
   `failed` so the next request reloads/reroutes. `GET /api/ps` passes through
   Ollama residency. `/status` carries per-member `healthy`/`last_health` + pools.

## Alternatives considered

- **One-name-one-model only** (rejected): no horizontal capacity/failover.
- **Always round-robin** (rejected as default): breaks cache locality; sticky
  keeps a conversation warm while still balancing new sessions.
- **Loading a model to health-check it** (rejected): violates lazy-load and wastes
  GPU/RAM; daemon/process liveness is checkable without a load.

## Consequences

- Each member keeps its own lifecycle, load lock, concurrency, and idle timer;
  `max_loaded_models` / per-model concurrency apply per member.
- Sticky adds a small bounded LRU map (`route_sticky_max_entries`).
- Mid-stream failures after the first byte propagate (the response has begun) —
  only pre-first-byte stream failover is transparent.
