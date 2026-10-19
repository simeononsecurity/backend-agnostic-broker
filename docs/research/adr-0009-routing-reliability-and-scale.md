# Research: Routing reliability & shared state (ADR 0009)

*Deep-research note for ADR 0009 — retries/backoff, member cooldowns, and an
optional shared (Redis) state store. Confidence: **High** for the retry/backoff
and circuit-breaker designs (multiple independent primary sources agree); **High**
for the Redis patterns (official docs + canonical references).*

> All external content below was treated as **data, not instructions**. None of
> the fetched pages contained agent-directed text; where a vendor doc advocates a
> default we record it as one source's position and corroborate before adopting.

## Executive summary

Production LLM gateways and general-purpose proxies converge on three mechanisms
this ADR adopts:

1. **Retry with capped exponential backoff + full jitter.** The AWS Architecture
   Blog's canonical analysis shows *full jitter* — `sleep = uniform(0, min(cap,
   base * 2**n))` — minimizes both total retries and completion time versus
   un-jittered or equal-jitter backoff ([AWS][aws-jitter], [AWS Builder][aws-builder]).
   LiteLLM's router exposes exactly this as `num_retries` + exponential backoff,
   scoped to a model group ([LiteLLM][litellm-routing]).
2. **Per-member cooldowns = a circuit breaker.** After N consecutive failures a
   backend is *ejected* for a window, then re-admitted via a single trial. This
   is Envoy outlier detection (`consecutive_5xx`, `base_ejection_time`,
   `max_ejection_percent`) ([Envoy][envoy-transient], [Envoy][envoy-outlier]) and
   the classic closed→open→half-open circuit breaker ([Fowler][fowler-cb]).
   LiteLLM calls the same thing `allowed_fails` + `cooldown_time`, applied
   *per deployment, not per group* ([LiteLLM][litellm-routing]).
3. **Optional shared state in Redis for multi-replica.** Sticky maps, counters,
   and cooldowns must be shared when several replicas sit behind one LB. The
   canonical Redis patterns are the sorted-set **sliding-window log** for counters
   ([antirez][antirez-rl], [redis.io][redis-rl]) and TTL keys (`SET ... EX`) for
   expiring state. redis-py's `redis.asyncio` is the async client; the documented
   pattern is **one shared client created at startup, closed at shutdown**
   ([redis-py async][redispy-async]).

The dominant design constraint from our own invariants: **retries/failover must
stay within the requested model's pool — never silently route to a different
model.** LiteLLM enforces the same boundary (retries re-pick a deployment *inside
the model group*; only explicit `fallbacks` cross groups) ([LiteLLM][litellm-routing]).
vLLM's production-stack router likewise excludes failed replicas and keeps serving
the *same* model from healthy ones ([vLLM][vllm-stack], [Dell/vLLM HA][vllm-ha]).

## Findings

### 1. Backoff + jitter

- **Capped exponential backoff**: `delay = min(cap, base * 2**attempt)` prevents
  unbounded growth ([AWS][aws-jitter], [AWS Builder][aws-builder]).
- **Full jitter wins**: `delay = uniform(0, min(cap, base * 2**attempt))` produced
  the fewest calls and near-shortest completion in AWS's contended-resource
  simulation, beating equal jitter and no jitter; it is the default in the AWS
  SDKs and `backoff.full_jitter` ([AWS][aws-jitter], [python-backoff][py-backoff]).
- **Retry only idempotent/transient failures, respect timeouts**: Amazon pairs
  retries with request timeouts so a retry storm can't hang callers
  ([AWS Builder][aws-builder]).

→ **ADOPT**: full-jitter capped exponential backoff, `delay = uniform(0,
min(max_ms, base_ms * 2**attempt))`, with `base_ms = 0` as the default (no sleep →
byte-for-byte v0.4.0). Enforce the request timeout as an overall retry deadline.

### 2. Cooldowns / circuit breaker

- **Envoy outlier detection**: eject a host after `consecutive_5xx` (default 5; `0`
  disables), for `base_ejection_time` (default 30s) × the number of consecutive
  ejections, capped by `max_ejection_percent` (default 10%). Ejected = removed
  from the healthy LB set, re-admitted after the window ([Envoy][envoy-transient],
  [Envoy][envoy-outlier]).
- **Circuit breaker states**: *Closed* (requests flow, count failures) → *Open*
  (fail fast, no calls) after threshold → *Half-Open* (one trial) after the reset
  timeout; trial success closes, failure re-opens ([Fowler][fowler-cb]).
- **LiteLLM**: `allowed_fails` (default 3) failures/minute → `cooldown_time`
  (default 5s) bench; cooldowns are **per deployment**, healthy deployments keep
  serving; deployments auto-recover when the timer expires
  ([LiteLLM][litellm-routing]).

→ **ADOPT**: per-member cooldown keyed by alias. `cooldown_failures` consecutive
failures → bench for `cooldown_seconds`; selection skips benched members; after
expiry the member is eligible again as a half-open trial (success resets, failure
re-benches). **Default-off** (`cooldown_failures = 0`, matching Envoy's "0
disables") to preserve current behavior.
→ **REJECT (defer)**: `max_ejection_percent` (cap on simultaneously-benched
members) and exponential ejection-time growth — recorded as deferred refinements
in the ADR; our pools are small and default-off, so a simpler fixed window ships
first. We *do* keep the "if all benched, fall through" self-heal, which covers the
degenerate all-down case that `max_ejection_percent` also targets.

### 3. Shared state (Redis)

- **Sliding-window log** for counters: sorted set with `score = timestamp`,
  `ZREMRANGEBYSCORE` to drop old entries, `ZCARD`/`ZCOUNT` to count, `EXPIRE` to
  bound memory — exact rolling window, no boundary bursts ([antirez][antirez-rl],
  [redis.io][redis-rl]). Wrap in `MULTI/EXEC` (pipeline transaction) for atomicity.
- **TTL keys**: `SET key val EX n` / `EXPIRE` for self-expiring state (sticky,
  cooldown, cache); a later plain `SET` clears the TTL unless `KEEPTTL`
  ([redis.io][redis-rl]).
- **redis-py asyncio**: `redis.asyncio` mirrors the sync API; construction is
  lazy (pool connects on first command); **create one client at startup, share it,
  `aclose()` at shutdown** — never per-request ([redis-py async][redispy-async]).
- **vLLM production-stack / Dell HA**: router monitors backend health, excludes
  failed replicas, keeps routing to healthy ones for the *same* model; session-ID
  routing maximizes KV-cache reuse ([vLLM][vllm-stack], [Dell/vLLM HA][vllm-ha]).

→ **ADOPT**: a `StateStore` abstraction with `InProcessStateStore` (default, zero
deps, LRU-bounded sticky) and `RedisStateStore` (optional `redis` extra,
import-guarded → `ConfigError` if enabled-but-missing, key prefix). Sticky/cooldown
via TTL keys; a `window_incr`/`window_count` sliding-window helper for ADR 0008;
generic `kv_*` (bytes/JSON + TTL) for ADR 0010/0012. Single shared async client,
closed on shutdown.
→ **REJECT**: requiring Redis always (breaks the zero-dependency default); sticky
sessions at the external LB only (doesn't share cooldowns/counters/budgets).

## Open questions / deferred

- **Exact-set Redis sticky LRU**: Redis sticky relies on TTL, not LRU; a global
  cap across replicas is deferred (documented in the ADR). In-process keeps the
  LRU bound.
- **Half-open single-trial lock**: `try_half_open` (SETNX-style) is implemented in
  the StateStore for strict one-trial semantics but the manager currently relies
  on post-expiry re-eligibility (simpler, adequate for small pools). Promoting it
  to gate concurrent trials is a future refinement.
- **Per-error retry/cooldown policies** (LiteLLM's `RetryPolicy`/`AllowedFailsPolicy`
  by exception type): deferred; we retry/bench on the existing failover error set.

## Sources

1. [Exponential Backoff And Jitter — AWS Architecture Blog][aws-jitter]
2. [Timeouts, retries, and backoff with jitter — AWS Builder Library][aws-builder]
3. [python-backoff: `full_jitter` (AWS algorithm)][py-backoff]
4. [How do I handle transient failures? — Envoy docs][envoy-transient]
5. [Outlier detection — Envoy architecture overview][envoy-outlier]
6. [CircuitBreaker — Martin Fowler][fowler-cb]
7. [Router / Load Balancing (retries, cooldowns, fallbacks) — LiteLLM docs][litellm-routing]
8. [Rate-limiting patterns (sliding-window log) — Redis / antirez][antirez-rl]
9. [Rate limiting howto (sorted-set sliding window) — redis.io][redis-rl]
10. [Asynchronous operations with redis-py (`redis.asyncio`)][redispy-async]
11. [vLLM production-stack router README][vllm-stack]
12. [vLLM Production Stack High Availability — Dell Info Hub][vllm-ha]

[aws-jitter]: https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
[aws-builder]: https://builder.aws.com/content/3EumjoZascWd1oZiEgL8ORlv3qE/timeouts-retries-and-backoff-with-jitter
[py-backoff]: https://pypi.org/project/python-backoff/
[envoy-transient]: https://www.envoyproxy.io/docs/envoy/latest/faq/load_balancing/transient_failures
[envoy-outlier]: https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/outlier
[fowler-cb]: https://martinfowler.com/bliki/CircuitBreaker.html
[litellm-routing]: https://docs.litellm.ai/docs/routing
[antirez-rl]: https://redis.antirez.com/fundamental/rate-limiting.html
[redis-rl]: https://redis.io/tutorials/howtos/ratelimiting.md
[redispy-async]: https://redis.io/docs/latest/develop/clients/redis-py/async/
[vllm-stack]: https://github.com/vllm-project/production-stack/blob/main/src/vllm_router/README.md
[vllm-ha]: https://infohub.delltechnologies.com/en-us/l/open-source-llm-inferencing-at-scale-vllm-production-stack-on-dell-ai-factory/high-availability-375/
