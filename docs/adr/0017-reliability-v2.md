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

4. **Per-error retry/cooldown policies + typed upstream errors.** A new
   `broker.reliability` module classifies failures into `timeout` / `connection` /
   `server_error` / `backpressure` (429) / `client_error` (4xx) / `other`. For this
   to apply to *real* backends, every HTTP adapter raises a typed
   `UpstreamHTTPError(upstream_status=..., headers=...)` when the backend
   **responds** with a status `>= 400` (chat non-stream + pre-first-byte stream,
   completions/responses via chat, and embeddings) — distinct from
   `BackendUnavailableError`, which now means only a *transport* failure (connect
   error, read timeout, unreachable). `classify_error` buckets an upstream error by
   its status: `408` → timeout, `429` → backpressure, other `4xx` → client_error,
   `5xx` → server_error. Upstream bodies are sanitized (control-stripped, truncated
   to ~200 chars, no secret reflection) before any of the message reaches a client.
   A `RetryPolicy` built from per-class settings answers *retryable?* (sleep-retry),
   *fails over?* (try another member), *counts toward cooldown?*, and *per-class
   attempt cap?*. **Defaults reproduce ADR 0009 exactly**: timeout + connection +
   server_error are retried and count toward cooldown; backpressure and
   client/other are not. **4xx client errors are hard-wired non-retryable, never
   failed over, and never counted** — the broker returns the upstream `4xx` to the
   caller with type `upstream_client_error` (a client's own bad request is **never**
   reported as a `502` and never retried across the pool). `BROKER_RETRY_ON_*` /
   `BROKER_COOLDOWN_ON_*` toggle the other classes, and
   `BROKER_RETRY_MAX_ATTEMPTS_BY_CLASS` sets per-class attempt caps.

   **Backpressure / `Retry-After` (RFC 9110: delay-seconds or HTTP-date).** A `429`
   and a `503` always fail over to a healthy member **immediately** (no sleep) and
   bench the throttled member for `min(Retry-After, cooldown_max_seconds or
   retry_after_max_seconds)`; a lone throttled member returns its `429`/`Retry-After`
   to the client rather than sleeping (clients self-pace), while a `5xx`
   (server_error) sleep-retries a lone member within budget. Every `Retry-After`
   wait is clamped to **both** `BROKER_RETRY_AFTER_MAX_SECONDS` (default 120s) and
   the remaining request deadline, so neither a hostile header nor
   `request_timeout_seconds = 0` (unbounded) can ever produce an unbounded sleep.
   Streaming applies the same policy to the pre-stream select/ready phase and still
   never retries once bytes are flowing (a pre-first-byte `4xx` is surfaced in the
   SSE error frame, upstream called once, no bench).

5. **Redis sticky global LRU bound (`BROKER_REDIS_STICKY_MAX_ENTRIES`, 0 =
   TTL-only).** A sorted-set index scored by last pin (= last use) caps the total
   sticky entries one Redis prefix holds; overflow is evicted with `ZPOPMIN` and
   the orphaned sticky keys deleted. Shared across replicas. `0` keeps v0.5.0's
   TTL-only behavior; in-process sticky keeps its `OrderedDict` LRU.

New reliability state is surfaced under `reliability` in `/status` (now including
`retry_after_max_seconds`), and the per-member cooldown record — `failures`,
`in_cooldown`, `recovering`, `until`, `seconds_remaining`, `ejections`, and (when
single-trial half-open is enabled) `half_open_trial` — appears in both `/status`
and `/healthz/backends`.

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
  using the **real upstream status** (never retry/charge a 4xx; an upstream 4xx is
  returned to the caller as that 4xx, not a 502; honor upstream backpressure with
  immediate failover); multi-replica sticky has a hard memory bound. All opt-in;
  defaults unchanged.
- **Behavior change (documented)** — an upstream HTTP `4xx` (e.g. a context-length
  `400`) now returns that `4xx` with type `upstream_client_error` and is called
  exactly once, instead of being collapsed to `502 backend_unavailable` and retried
  across every pool member (the pre-fix behavior, also present in v0.5.0). An
  upstream `429`/`503` now fails over immediately and benches the throttled member.
  Any v0.5.0 test asserting the old 502-on-upstream-4xx is an intended change.
- **Negative** — more routing configuration surface; `_select_member` carries the
  ejection-cap + half-open logic; a second cooldown sub-structure (ejection count)
  to maintain across both stores; the Redis sticky LRU adds a write on the sticky
  write path when enabled; a typed-upstream-error path in every adapter.
- **Risks** — a misconfigured cap could keep a known-bad member eligible (mitigated:
  it only reinstates the soonest-to-recover, and a genuinely failing member is
  re-benched on its next failure); a large or hostile `Retry-After` could stall a
  retry (mitigated: clamped to **both** `retry_after_max_seconds` and the request
  deadline — bounded even when `request_timeout_seconds = 0`); exponential growth
  could over-bench a member (mitigated: `cooldown_max_seconds` cap + decay).

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
- `tests/test_error_policy.py` — the classification matrix (incl. upstream-status
  typing), `Retry-After` parsing (seconds, HTTP-date, absent/invalid), policy
  defaults = ADR 0009, 4xx never retried/counted even if configured, backpressure
  **fails over to a healthy member** (and a lone member returns 429, no sleep),
  cooldown counts only configured classes, upstream `Retry-After` immediate failover
  vs. clamped sleep-only-when-no-member, and per-class cap stops retrying.
- `tests/test_upstream_errors.py` — **end-to-end** against a local
  `ThreadingHTTPServer` upstream (not hand-built exceptions): upstream
  `400/404/413/422` → client `4xx` `upstream_client_error`, called once, no bench;
  message sanitized + truncated; `429`/`503`+`Retry-After` → immediate failover +
  throttled member benched; single-member and `request_timeout = 0` + huge
  `Retry-After` both bounded; streaming pre-first-byte `4xx` same as non-stream;
  embeddings `4xx` → client `4xx`; non-finite reliability knobs rejected.
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
