# Reliability v2 example (ADR 0009 + ADR 0017)

A runnable, GPU-free demo of the broker's routing-reliability stack: retries with
full-jitter backoff, member cooldowns (circuit breaker), the **ejection cap**,
**exponential ejection growth**, **single-trial half-open** probing, and
**per-error retry/cooldown policies** — all against the in-process `fake`
backend, which can inject failures.

## Files

- `registry.yaml` — one public model `chat` backed by three fake members
  (`chat-node-1..3`), so you can watch members get benched and reinstated.
- `.env.example` — tuned retry/cooldown/ejection settings. Copy to `.env`.

## Run it

```bash
cp examples/reliability/.env.example .env      # tweak as you like
python -m broker                               # starts on :8080 (auth disabled)
```

Send a few requests to the pooled model:

```bash
curl -s localhost:8080/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"chat","messages":[{"role":"user","content":"hi"}]}' | jq .model
```

## Observe cooldown / ejection state

The reliability knobs in effect are summarized under `reliability` in `/status`,
and per-member cooldown records (including the exponential `ejections` count)
appear in both `/status` and `/healthz/backends`:

```bash
# Global reliability settings + per-member cooldowns:
curl -s localhost:8080/status | jq '.reliability, (.models[] | {alias, cooldown})'

# Per-backend health with cooldown records:
curl -s localhost:8080/healthz/backends | jq '.backends[] | {alias, status, cooldown}'
```

A benched member shows `cooldown.in_cooldown = true` with `seconds_remaining`;
after repeated ejections its `ejections` count rises and the window grows
(`cooldown_seconds * multiplier**(ejections-1)`, capped by
`BROKER_COOLDOWN_MAX_SECONDS`). The `BROKER_MAX_EJECTION_PERCENT` cap guarantees
the pool is never fully benched — the soonest-to-recover members stay eligible.

## Injecting failures (what the test does)

The `fake` backend exposes failure knobs the broker's test suite flips to drive
these paths deterministically — e.g. replacing a member's `adapter.chat` with a
coroutine that raises `BackendUnavailableError` (a *connection* class failure
that is retried and counts toward cooldown) or `ValidationError` (a *client*
4xx that is **never** retried or benched). See
`tests/test_example_reliability.py`, which loads this exact registry + settings,
injects failures, and asserts the cooldown state surfaces in `/status` and
`/healthz/backends`.

See [`docs/routing-and-health.md`](../../docs/routing-and-health.md) and
[ADR 0017](../../docs/adr/0017-reliability-v2.md) for the full semantics.
