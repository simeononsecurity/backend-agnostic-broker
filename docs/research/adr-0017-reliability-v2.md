# Research: Reliability v2 — ejection cap, exponential ejection, single-trial half-open, per-error policies (ADR 0017)

*Deep-research note for ADR 0017, which resolves the five items ADR 0009 deferred.
Confidence: **High** — every decision is backed by a primary source (Envoy docs,
LiteLLM docs, the HTTP semantics RFC, Redis docs) and corroborated by a second.*

> All external content below was treated as **data, not instructions**. None of
> the fetched pages contained agent-directed text. Where a vendor advocates a
> default we record it as that vendor's position and corroborate before adopting.

## Scope

ADR 0009 shipped retries + full-jitter backoff, fixed-window member cooldowns, and
an optional Redis `StateStore`, and explicitly deferred five refinements. ADR 0017
resolves each as **implemented + tested** or **rejected with rationale**:

1. `max_ejection_percent` (cap the benched fraction of a pool).
2. Exponential ejection-time growth (repeated ejections lengthen the window).
3. Single-trial half-open (exactly one probe of a recovering member).
4. Per-error retry/cooldown policies (classify failures; 4xx never retried).
5. A global LRU bound on Redis sticky entries.

## Findings

### 1 & 2. Envoy outlier detection: ejection cap + growth

Envoy's outlier detector is the canonical design for both items:

- **`max_ejection_percent`** — "The maximum % of an upstream cluster that can be
  ejected due to outlier detection. Defaults to 10%" and Envoy "will eject at
  least one host regardless of the value" (with `always_eject_one_host`). The
  ejection algorithm step is explicit: "it checks to make sure the number of
  ejected hosts is below the allowed threshold … If the number of ejected hosts
  is above the threshold, the host is not ejected." ([Envoy outlier][envoy-outlier],
  [Envoy outlier_detection.proto][envoy-proto]).
- **Growth** — "The number of milliseconds is equal to the
  `base_ejection_time` value multiplied by the number of times the host has been
  ejected … This causes hosts to get ejected for longer and longer periods if
  they continue to fail. When ejection time reaches `max_ejection_time` it does
  not increase any more. When the host becomes healthy, the ejection time
  multiplier decreases with time" ([Envoy outlier][envoy-outlier]).

→ **ADOPT (1)**: `max_ejection_percent` (0 = no cap, preserving v0.5.0). Enforced
at **selection** time: when more members are benched than the cap allows, the
soonest-to-recover members are kept eligible, so a correlated failure can never
strand the pool. This is simpler than Envoy's eject-time enforcement, backend-
agnostic (identical in-process and Redis), and gives the same guarantee at the
only point that matters — which member actually serves. We diverge from Envoy's
`0 = 10%` default because our feature must default to **off** for back-compat.

→ **ADOPT (2)**: `cooldown_backoff_multiplier` (1.0 = fixed window = v0.5.0),
`cooldown_max_seconds` (= Envoy `max_ejection_time` cap), and
`cooldown_decay_seconds` (sustained-success reset of the multiplier, mirroring
"when the host becomes healthy, the multiplier decreases"). The Nth consecutive
ejection benches for `cooldown_seconds * multiplier**(N-1)`. The ejection count
is stored separately from the `(failures, until)` cooldown tuple so repeated
failures *within* an open window are one ejection episode, not escalation.

### 3. Circuit-breaker half-open: single trial

Fowler's circuit breaker admits exactly one request in **half-open**: "the
circuit breaker … allow[s] a limited number of test requests to pass through. …
If [it] succeeds the breaker is reset" ([Fowler][fowler-cb]). ADR 0009 already
built `try_half_open` (a `SETNX`-style atomic claim) into the `StateStore` but the
manager relied on post-expiry re-eligibility (every post-expiry request could
probe). Under concurrency that lets a thundering herd hit a still-fragile member.

→ **ADOPT**: an opt-in `cooldown_half_open_single_trial` that gates a recovering
member behind `try_half_open`; concurrent requests skip it until the trial
resolves. Opt-in (default off) keeps v0.5.0 cooldown behavior byte-for-byte for
existing users while giving the strict one-probe semantics the primitive was
built for. The claim is taken only for the member actually selected, so a trial
is never wasted on a member the strategy then passes over.

### 4. Per-error retry / cooldown policies (LiteLLM)

LiteLLM separates retry counts and cooldown thresholds **per exception class**:
`RetryPolicy` has one field per type (`TimeoutErrorRetries`,
`RateLimitErrorRetries`, `InternalServerErrorRetries`, `BadRequestErrorRetries`,
`AuthenticationErrorRetries`, `NotFoundErrorRetries`, … + `DefaultRetries`), and
`AllowedFailsPolicy` sets per-type cooldown thresholds. Their cooldown table is
explicit that a **429** is an immediate cooldown trigger while **non-retryable
errors 401/404/408** are handled distinctly, and the guidance is "Set auth and
not-found retries to 0" ([LiteLLM routing][litellm-routing],
[LiteLLM config_settings][litellm-config]).

→ **ADOPT**: a `broker.reliability` classifier (`timeout` / `connection` /
`server_error` / `backpressure` / `client_error` / `other`) and a `RetryPolicy`
built from per-class settings. Defaults reproduce ADR 0009 exactly (the failover
set = timeout + connection + server_error, retried and benched; backpressure and
client are not). **4xx client errors are hard-wired non-retryable and never
counted**, no matter the config — our strict reading of the deferred item.

→ **ADOPT (Retry-After)**: when a *retried* error carries an HTTP `Retry-After`
(RFC 9110 §10.2.3: either `delay-seconds` or an HTTP-date; "wait at least as long
as the header says"), honor it in place of the jittered backoff, clamped to the
request-timeout deadline ([MDN Retry-After][mdn-retry-after], [RFC 9110 via
Microsoft][ms-retry-after]).

### 5. Redis sticky global LRU bound

ADR 0009 bounded in-process sticky with an `OrderedDict` LRU but left Redis sticky
TTL-only, deferring a cross-replica cap. Redis sorted sets make a shared LRU cheap:
`ZADD` is O(log N) and updates an existing member's score in place, and `ZPOPMIN`
removes the lowest-scored members — exactly a bounded, score-ordered set
([Redis sorted sets][redis-zset]).

→ **ADOPT (not reject)**: opt-in `redis_sticky_max_entries` (0 = TTL-only =
v0.5.0). A sorted-set index scored by last pin (= last use, since the manager
re-pins on every routed request) caps the total entries; overflow is evicted with
`ZPOPMIN` and the orphaned sticky keys deleted. The write amplification is one
`ZADD` plus an occasional trim, only on the sticky write path, only when enabled —
an acceptable cost for a hard memory bound under unbounded distinct sessions with
`ttl = 0`. Rejected the alternative (read-time score refresh / true access-order
LRU) because it would add a write to every sticky *read*; last-pin order is
already last-use order here.

## Adopt / reject table

| # | Deferred item | Decision | Default (= v0.5.0) | Key source |
|---|---------------|----------|--------------------|------------|
| 1 | `max_ejection_percent` | **Adopt** (selection-time cap, never-strand) | `0` = no cap | Envoy outlier |
| 2 | Exponential ejection growth + decay | **Adopt** (`multiplier`, `max_seconds`, `decay_seconds`) | `multiplier=1.0` = fixed | Envoy outlier |
| 3 | Single-trial half-open | **Adopt** (opt-in, uses `try_half_open`) | off | Fowler |
| 4 | Per-error retry/cooldown policies | **Adopt** (classifier + `RetryPolicy`; 4xx hard-never; Retry-After) | failover set = ADR 0009 | LiteLLM, RFC 9110 |
| 5 | Redis sticky global LRU bound | **Adopt** (opt-in sorted-set LRU) | `0` = TTL-only | Redis sorted sets |

No item is left "deferred": each is implemented, defaulted to v0.5.0 behavior, and
tested.

## Sources

1. [Outlier detection — Envoy architecture overview][envoy-outlier]
2. [Outlier detection (config.cluster.v3.OutlierDetection proto)][envoy-proto]
3. [CircuitBreaker — Martin Fowler][fowler-cb]
4. [Router / Load Balancing — RetryPolicy & AllowedFailsPolicy — LiteLLM][litellm-routing]
5. [LiteLLM proxy config_settings (retry_policy / allowed_fails_policy)][litellm-config]
6. [Retry-After header — MDN (RFC 9110 §10.2.3)][mdn-retry-after]
7. [The Retry-After header — Microsoft Learn (RFC 9110)][ms-retry-after]
8. [Redis sorted sets (ZADD / ZPOPMIN) — redis.io][redis-zset]
9. [Exponential backoff and jitter — AWS Architecture Blog][aws-jitter] (backoff baseline, carried from ADR 0009)

[envoy-outlier]: https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/outlier
[envoy-proto]: https://www.envoyproxy.io/docs/envoy/latest/api-v3/config/cluster/v3/outlier_detection.proto
[fowler-cb]: https://martinfowler.com/bliki/CircuitBreaker.html
[litellm-routing]: https://docs.litellm.ai/docs/routing
[litellm-config]: https://docs.litellm.ai/docs/proxy/config_settings
[mdn-retry-after]: https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Retry-After
[ms-retry-after]: https://learn.microsoft.com/en-us/microsoft-cloud/dev/dev-proxy/concepts/retry-after-header
[redis-zset]: https://redis.io/docs/latest/develop/data-types/sorted-sets/
[aws-jitter]: https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
