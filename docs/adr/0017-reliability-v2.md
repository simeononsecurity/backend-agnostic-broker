# ADR 0017: Reliability v2 — ejection cap, exponential ejection, single-trial half-open, per-error policies

- Status: Accepted
- Date: 2026-10-19
- Supersedes: none
- Extends: ADR 0009 (routing reliability & scale)
- Related: ADR 0003 (pools/sticky/health); `docs/routing-and-health.md`,
  `docs/architecture.md`, `docs/configuration.md`; research:
  `docs/research/adr-0017-reliability-v2.md`. Prior art: Envoy outlier detection,
  LiteLLM `RetryPolicy`/`AllowedFailsPolicy`, HTTP `Retry-After` (RFC 9110),
  Redis sorted sets, the circuit-breaker pattern (Fowler).

## Context

ADR 0009 shipped retries + full-jitter backoff, fixed-window member cooldowns, and
an optional Redis `StateStore`, and **explicitly deferred** five refinements. This
ADR resolves all five so none remains "future work": each is either implemented +
tested or rejected with a concrete rationale. The hard invariant is unchanged —
**retries/failover stay within the requested public model's pool, never another
model** — and **every new knob defaults to v0.5.0 behavior** (the v0.5.0 test
suite passes unchanged against this branch).

The deferred items:

1. `max_ejection_percent` — cap the fraction of a pool that may be benched at once
   so a correlated failure can't eject every member.
2. Exponential ejection-time growth — repeated ejections of the same member
   lengthen its cooldown, decaying after sustained success.
3. Single-trial half-open — exactly one request probes a recovering member.
4. Per-error retry/cooldown policies — classify failures and apply per-class retry
   and cooldown-counting rules; 4xx client errors never retried or counted; honor
   upstream `Retry-After`.
5. Redis sticky global LRU bound.

## Decision

All five are **adopted**, each opt-in with a default that reproduces v0.5.0.

1. **Ejection cap (`BROKER_MAX_EJECTION_PERCENT`, 0 = no cap).** At member
   selection, at most `floor(pct * N / 100)` of the pool is treated as benched;
   when more are in cooldown, the **soonest-to-recover** members are reinstated so
   the pool is **never stranded** (Envoy's "eject at least one host regardless").
   Enforced at selection (not ejection) time so the guarantee is identical
   in-process and under Redis and applies at the only point that matters — which
   member serves. `0` keeps v0.5.0's behavior (any/all may be benched, with the
   existing all-benched fall-through).

2. **Exponential ejection growth + decay.** The Nth consecutive ejection of a
   member benches it for `cooldown_seconds * BROKER_COOLDOWN_BACKOFF_MULTIPLIER **
   (N-1)`, capped by `BROKER_COOLDOWN_MAX_SECONDS` (Envoy `base_ejection_time ×
   consecutive`, capped by `max_ejection_time`). The ejection count decays — a
   successful serve resets it once the member has been healthy for
   `BROKER_COOLDOWN_DECAY_SECONDS` (0 = reset on any success). The ejection count
   and last-ejection timestamp are stored **separately** from the `(failures,
   until)` cooldown record, so repeated failures *within* one open window are a
   single ejection episode and the existing cooldown record shape is unchanged.
   `multiplier = 1.0` (default) is a fixed window = v0.5.0.

3. **Single-trial half-open (`BROKER_COOLDOWN_HALF_OPEN_SINGLE_TRIAL`, default
   off).** When on, a recovering member (window elapsed, record still open) is
   admitted to exactly one probe via `StateStore.try_half_open` (atomic in-process
   and via Redis `SET NX`); concurrent requests skip it until the trial resolves
   (success closes the breaker, failure re-benches with growth). The trial lock is
   claimed only for the member actually selected, never wasted. Default-off
   preserves ADR 0009's post-expiry re-eligibility for existing cooldown users.

4. **Per-error retry/cooldown policies.** A new `broker.reliability` module
   classifies every `BrokerError` into `timeout` / `connection` / `server_error` /
   `backpressure` (429) / `client_error` (4xx) / `other`. A `RetryPolicy` built
   from per-class settings answers *retryable?*, *counts toward cooldown?*, and
   *per-class attempt cap?*. **Defaults reproduce ADR 0009 exactly**: timeout +
   connection + server_error are retried and count toward cooldown; backpressure
   and client/other are not. **4xx client errors are hard-wired non-retryable and
   never counted** — no configuration can change that. `BROKER_RETRY_ON_*` /
   `BROKER_COOLDOWN_ON_*` toggle the other classes (e.g. opt a 429 into retry), and
   `BROKER_RETRY_MAX_ATTEMPTS_BY_CLASS` sets per-class attempt caps. When a
   *retried* error carries an HTTP `Retry-After` (RFC 9110: delay-seconds or
   HTTP-date) and `BROKER_RETRY_RESPECT_RETRY_AFTER` is set (default on), the retry
   waits that long instead of the jittered backoff, clamped to the request-timeout
   deadline. Streaming applies the same policy to the pre-stream select/ready phase
   and still never retries once bytes are flowing.

5. **Redis sticky global LRU bound (`BROKER_REDIS_STICKY_MAX_ENTRIES`, 0 =
   TTL-only).** A sorted-set index scored by last pin (= last use) caps the total
   sticky entries one Redis prefix holds; overflow is evicted with `ZPOPMIN` and
   the orphaned sticky keys deleted. Shared across replicas. `0` keeps v0.5.0's
   TTL-only behavior; in-process sticky keeps its `OrderedDict` LRU.

New reliability state is surfaced under `reliability` in `/status`, and the
per-member cooldown record (now including the `ejections` count) appears in both
`/status` and `/healthz/backends`.

## Alternatives considered

- **Enforce the ejection cap at eject time (Envoy-exact).** Rejected: the
  `StateStore.record_failure` primitive is per-key and has no pool view; enforcing
  at selection is backend-agnostic and gives the same never-strand guarantee where
  it counts.
- **Make single-trial half-open always-on when cooldowns are enabled.** Rejected
  as the default: it would change behavior for existing cooldown users. Shipped
  opt-in instead (the invariant is "new knobs default to current behavior").
- **Reset the ejection multiplier on every success (no decay knob).** Kept as the
  *default* (`decay_seconds = 0`) but added the knob so operators can punish a
  member that flaps between success and failure.
- **Reject the Redis sticky LRU bound** (cost vs benefit, since TTL already bounds
  it). Rejected the rejection: sorted-set LRU is O(log N), opt-in, and only adds a
  write on the sticky write path — a reasonable price for a hard memory bound when
  `ttl = 0` meets unbounded distinct sessions.
- **Read-time score refresh for true access-order sticky LRU.** Rejected: it adds
  a write to every sticky read; last-pin order already equals last-use order
  because the manager re-pins on every routed request.
- **A per-class `RetryPolicy` object in the registry (LiteLLM-style nested dict).**
  Rejected for config surface: flat `BROKER_*` booleans + one JSON map
  (`RETRY_MAX_ATTEMPTS_BY_CLASS`) cover the same ground with the project's existing
  settings idiom.

## Consequences

- **Positive** — a correlated outage can no longer bench a whole pool; a
  persistently flapping member is ejected for exponentially longer; recovering
  members get one careful probe instead of a herd; failures are handled by class
  (never retry/charge a 4xx; honor upstream backpressure); multi-replica sticky has
  a hard memory bound. All opt-in; defaults unchanged.
- **Negative** — more routing configuration surface; `_select_member` carries the
  ejection-cap + half-open logic; a second cooldown sub-structure (ejection count)
  to maintain across both stores; the Redis sticky LRU adds a write on the sticky
  write path when enabled.
- **Risks** — a misconfigured cap could keep a known-bad member eligible (mitigated:
  it only reinstates the soonest-to-recover, and a genuinely failing member is
  re-benched on its next failure); a large `Retry-After` could stall a retry
  (mitigated: clamped to the request-timeout deadline); exponential growth could
  over-bench a member (mitigated: `cooldown_max_seconds` cap + decay).

## Confirmation

Verified by the test suite (run from the worktree; Redis paths use `fakeredis` and
skip cleanly where the optional extra is absent). New tests:

- `tests/test_ejection_cap.py` — the cap function (keep soonest-to-recover
  eligible), selection honors it, **never-strand** when a whole pool is benched,
  and `0` falls through to the ADR 0009 self-heal.
- `tests/test_cooldown_backoff.py` — exponential window growth, `max_seconds` cap,
  `multiplier = 1.0` fixed window, repeated-failure-within-window is one ejection,
  decay on any success and decay-requires-sustained-success, `ejections` surfaced;
  parametrized over in-process **and** fakeredis, plus an end-to-end manager check.
- `tests/test_half_open.py` — `try_half_open` single-winner under 20 concurrent
  claims (both stores), release on success, and manager single-trial gating:
  concurrent probes routed away from the held member, success closes / failure
  re-benches, and default-off allows multiple probes.
- `tests/test_error_policy.py` — the classification matrix, `Retry-After` parsing
  (seconds, HTTP-date, absent/invalid), policy defaults = ADR 0009, 4xx never
  retried/counted even if configured, backpressure opt-in, per-class attempt caps,
  and manager integration (4xx not retried/benched, 429 retried only when opted in,
  cooldown counts only configured classes, upstream `Retry-After` honored, per-class
  cap stops retrying).
- `tests/test_redis_sticky_lru.py` — LRU eviction of least-recently-pinned,
  re-pin refreshes position, unbounded keeps everything, bounded `sticky_len` uses
  the index (fakeredis).
- `tests/test_config.py` — every new knob's defaults preserve v0.5.0 and invalid
  values are rejected fail-fast.
- `tests/test_example_reliability.py` — the `examples/reliability` demo loads,
  serves, and surfaces cooldown/ejection state in `/status` + `/healthz/backends`.

Gates: `ruff check` + `ruff format --check`, full `pytest` (shared venv and the
private `[dev,redis,otel]` extras venv), the Python 3.11 container gate incl.
`mypy`, and the v0.5.0 backward-compatibility suite (passes unchanged).
