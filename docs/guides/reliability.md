# How-to: retries, cooldowns, and ejection

Make a pool resilient to flaky members: transparently **retry** within the pool,
**cool down** (circuit-break) a member after repeated failures, grow the cooldown
window on repeat offenders, cap how much of the pool can be benched, and probe a
benched member with a single half-open trial before reinstating it. All of this is
**opt-in and default-off** — defaults reproduce v0.5.0 exactly.

Retries never cross to a **different model**; they only try other members of the
same pool.

## When to use

Turn these on for any multi-member pool in production. Leave them off for
single-member models or local trials.

## Minimal config

```bash
BROKER_RETRY_MAX_ATTEMPTS=4          # try up to 4 members with full-jitter backoff
BROKER_COOLDOWN_FAILURES=2           # bench a member after 2 consecutive failures
BROKER_COOLDOWN_SECONDS=15           # for 15s
```

## Full config

```bash
# Backoff
BROKER_RETRY_BACKOFF_BASE_MS=50
BROKER_RETRY_BACKOFF_MAX_MS=2000
# Ejection growth + safety cap (ADR 0017)
BROKER_MAX_EJECTION_PERCENT=50           # never bench >50% of the pool at once
BROKER_COOLDOWN_BACKOFF_MULTIPLIER=2.0   # 15s -> 30s -> 60s on repeat ejection
BROKER_COOLDOWN_MAX_SECONDS=120          # capped here
BROKER_COOLDOWN_DECAY_SECONDS=300        # reset the multiplier after 300s of success
# Single-trial half-open probe
BROKER_COOLDOWN_HALF_OPEN_SINGLE_TRIAL=true
# Per-error-class policy (ADR 0017): which failures retry / cool down
BROKER_RETRY_ON_TIMEOUT=true
BROKER_RETRY_ON_CONNECTION=true
BROKER_RETRY_ON_SERVER_ERROR=true
BROKER_RETRY_ON_BACKPRESSURE=false       # 429s: opt in to retry
BROKER_RETRY_RESPECT_RETRY_AFTER=true
BROKER_RETRY_MAX_ATTEMPTS_BY_CLASS={"timeout": 2}
```

A client error (4xx `ValidationError`) is **never** retried or counted toward
cooldown — only connection/timeout/5xx (and 429 if opted in) do.

## Verify

```bash
curl -s http://127.0.0.1:8080/status | python3 -c \
  'import sys,json; d=json.load(sys.stdin); print(d["reliability"]); \
   print([(m["alias"], m["cooldown"]["in_cooldown"]) for m in d["models"]])'
curl -s http://127.0.0.1:8080/healthz/backends | python3 -m json.tool
```

Expected: `/status.reliability` echoes your settings; a benched member shows
`cooldown.in_cooldown = true` with `seconds_remaining`, and its `ejections` count
rises as the window grows. The ejection cap guarantees the pool is never fully
benched.

## Troubleshooting

| Symptom | Fix |
|---|---|
| A member never recovers | Confirm `BROKER_COOLDOWN_MAX_SECONDS` and `DECAY_SECONDS`; the half-open probe reinstates on one success. |
| A 429'd upstream keeps failing fast | Set `BROKER_RETRY_ON_BACKPRESSURE=true` and `RESPECT_RETRY_AFTER=true`. |
| Whole pool benched | Set `BROKER_MAX_EJECTION_PERCENT` < 100 to always keep members eligible. |
| 4xx being retried | It isn't — 4xx is a client error and is never retried or counted. Check your error class. |

## Example

[examples/reliability](../../examples/reliability/) — injects failures and shows
the cooldown/ejection state in `/status` and `/healthz/backends`.

## Reference & decisions

- Reference: [routing and health](../routing-and-health.md) · [configuration](../configuration.md)
- ADRs: [0009 reliability & scale](../adr/0009-routing-reliability-and-scale.md) ·
  [0017 reliability v2](../adr/0017-reliability-v2.md)
