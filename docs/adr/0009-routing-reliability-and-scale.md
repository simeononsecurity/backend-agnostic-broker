# ADR 0009: Routing reliability & horizontal scale — retries/cooldowns + optional shared state

- Status: Accepted
- Date: 2026-10-18
- Supersedes: none
- Related: ADR 0003 (pools/sticky/health); `docs/routing-and-health.md`,
  `docs/architecture.md`; research: `docs/research/adr-0009-routing-reliability-and-scale.md`;
  enables ADR 0008 (per-key counters), ADR 0010 (prefix→member index), ADR 0012
  (response cache). Prior art: LiteLLM router, Envoy outlier detection, AWS
  backoff guidance, Redis rate-limiting patterns.

## Context

Two gaps vs production gateways:

1. **Reliability** — failover is a single hop within a pool; there is no
   configurable retry with backoff, and a failed member isn't temporarily taken
   out of rotation (cooldown). A flapping backend is retried on every request.
2. **Horizontal scale** — the sticky map, in-flight counters, health, and
   (future) rate limits all live in-process, so running multiple broker replicas
   behind a load balancer gives inconsistent routing/limits.

Hard constraint from our invariants: **retries and failover must stay within the
requested public model's pool — never silently route to a different model.** The
zero-dependency, lazy-load, non-loading-health, security-guard invariants must all
survive, and defaults must reproduce v0.4.0 behavior exactly.

The research note surveys the converging prior art: capped exponential backoff
with **full jitter** (AWS), circuit-breaker **cooldowns** / outlier ejection
(Envoy, Fowler, LiteLLM — *per deployment, within the group*), and **Redis**
sliding-window counters + TTL keys with a single shared `redis.asyncio` client.

## Decision

We add three capabilities, all **opt-in with defaults that reproduce v0.4.0**:

1. **Retries + full-jitter backoff.** `BROKER_RETRY_MAX_ATTEMPTS` caps the total
   serve attempts for a request; between attempts the manager waits
   `uniform(0, min(BROKER_RETRY_BACKOFF_MAX_MS, BROKER_RETRY_BACKOFF_BASE_MS *
   2**attempt))` (full jitter, the AWS-recommended algorithm). Retries re-select a
   member **within the same pool only** — never another model. Defaults
   (`RETRY_MAX_ATTEMPTS=0`, `BACKOFF_BASE_MS=0`) mean one failover pass per pool
   member with no sleep: byte-for-byte v0.4.0. Streaming retries only *before the
   first byte*; once a stream is emitting we never retry. Retrying respects the
   request timeout as an overall deadline.

2. **Member cooldowns (circuit breaker).** After `BROKER_COOLDOWN_FAILURES`
   consecutive failures a member is benched for `BROKER_COOLDOWN_SECONDS`;
   selection skips benched members; once the window elapses the member is eligible
   again as a half-open trial (a success resets its failure count, another failure
   re-benches it). When enabled, cooldown state is the authoritative benching
   signal (it supersedes the transient `healthy` flag); if *every* member is
   benched the pool falls through to the healthy-based tier so it can still
   self-heal. Default-off (`COOLDOWN_FAILURES=0`, mirroring Envoy's "0 disables"),
   and cooldown state is surfaced in `/status` and `/healthz/backends`.

3. **`StateStore` abstraction (in-process | optional Redis).** `broker/state.py`
   defines one async `StateStore` with: a sticky session map; atomic counters
   with TTL plus a sliding-window helper; cooldown records; and a generic
   bytes/JSON KV with TTL. `InProcessStateStore` is the default (exact current
   semantics, LRU-bounded sticky, zero new dependencies). `RedisStateStore` is
   selected when `BROKER_REDIS_URL` is set, namespaces keys with
   `BROKER_REDIS_PREFIX`, and uses a single shared `redis.asyncio` client closed
   at shutdown. `redis` is an optional extra (`pip install
   'backend-agnostic-broker[redis]'`), imported lazily and import-guarded with a
   clear `ConfigError` when enabled-but-missing. The manager's sticky map is
   migrated onto `StateStore`. The API is deliberately broader than today's needs
   — it is the shared-state platform for ADR 0008 (per-key rate-limit counters →
   `incr`/`window_incr`), ADR 0010 (prefix→member index → `kv_*`), and ADR 0012
   (response cache → `kv_*`).

## Alternatives considered

- **In-process only (status quo)** — simplest, but caps us at one replica for
  consistent routing/limits; rejected as the *only* option (kept as the default).
- **Always require Redis** — rejected; breaks the zero-dependency self-contained
  default. Redis is a soft dependency only when multi-replica is wanted.
- **Sticky sessions at the external LB** — helps session affinity but not shared
  rate limits/budgets/cooldowns; rejected as a substitute.
- **Equal / decorrelated jitter** — AWS data shows full jitter does the least work
  and is simplest to implement correctly; adopted over equal jitter, decorrelated
  deferred.
- **`max_ejection_percent` + exponential ejection growth (Envoy)** — deferred;
  pools are small and cooldowns default-off, so a fixed window ships first. The
  "all-benched → fall through" self-heal covers the degenerate case.
- **Per-error retry/cooldown policies (LiteLLM `RetryPolicy`/`AllowedFailsPolicy`)**
  — deferred; we retry/bench on the existing failover error set.

## Consequences

- **Positive** — configurable resilience (retry transient failures, bench flapping
  members) without changing default behavior; a single state abstraction unblocks
  ADRs 0008/0010/0012; multi-replica deployments get shared sticky/cooldowns via
  Redis.
- **Negative** — more routing configuration surface; a second (Redis) code path to
  maintain and test; Redis sticky relies on TTL rather than a global LRU bound.
- **Risks** — a misconfigured backoff could add latency (mitigated: default 0, a
  backoff cap, and a request-timeout deadline); enabling cooldowns changes the
  failover/`healthy` interaction (mitigated: default-off, documented, visible in
  `/status`); a Redis outage would degrade a multi-replica deployment (operators
  choose Redis knowingly; absent the URL nothing changes).

## Confirmation

Verified by the test suite (run from the worktree; Redis tests use `fakeredis`
and skip cleanly where the optional dep is absent):

- `tests/test_retry.py` — full-jitter backoff bounds; **default path is a single
  failover pass with no sleep**; retries stay **within the pool, never another
  model**; attempt cap respected; transient-failure recovery; **no retry once a
  stream has started emitting**.
- `tests/test_cooldown.py` — member benched after N consecutive failures and
  skipped by selection; half-open trial after expiry; success resets the failure
  count; cooldown visible in `/status` and `/healthz/backends`; default-off
  performs no benching.
- `tests/test_state.py` — `StateStore` contract parametrized over
  `InProcessStateStore` and `RedisStateStore` (fakeredis): sticky roundtrip + LRU
  bound, counters + sliding window, cooldown transitions, half-open single claim,
  bytes/JSON KV, and the factory's fail-fast `ConfigError` when Redis is enabled
  but missing.
- `tests/test_config.py` + existing routing/sticky/streaming suites stay green
  unchanged, proving defaults reproduce v0.4.0. Gates: `ruff check` + `ruff format
  --check` + full `pytest`.
