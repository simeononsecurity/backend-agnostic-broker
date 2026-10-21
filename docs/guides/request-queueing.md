# Request queueing under overload (ADR 0023)

When the system or GPUs are overloaded, the broker can **queue** requests —
bounded, timed, and fair — instead of failing them or blocking a concurrency
semaphore forever. This guide covers when to enable it, how to tune it, how to
read the headers and `/status`, and how it interacts with per-key governance.

Opt-in and **OFF by default**: with `BROKER_QUEUE_ENABLED` unset, behavior is
exactly v0.6.0.

## When to enable it

Enable the queue when short bursts over your serving capacity are better absorbed
as brief waits than returned as errors — interactive apps, bursty batch jobs,
shared multi-tenant keys. Leave it off when callers prefer a fast failure they
retry elsewhere (the default `load_concurrency_policy` already blocks on the
semaphore; `reject` already 429s).

## Enable + tune

```bash
BROKER_QUEUE_ENABLED=true
BROKER_QUEUE_MAX_DEPTH=128            # global waiting bound
BROKER_QUEUE_MAX_DEPTH_PER_MODEL=32  # per-model waiting bound
BROKER_QUEUE_MAX_WAIT_SECONDS=30     # give up after this long
BROKER_QUEUE_RETRY_AFTER_SECONDS=5   # advertised on 429/503
```

Tuning depth and wait:

- **Depth** caps how many requests may wait. Set it from how many you can serve
  within `MAX_WAIT`: roughly `per_model_concurrency × (MAX_WAIT / typical_service_time)`.
  Too high and clients wait near the timeout only to be served late; too low and
  you shed load earlier (429 `queue_full`). A new request over the bound is
  rejected **immediately** with 429 + `Retry-After` — it never joins the queue.
- **Wait** caps the tail latency a caller will tolerate. On timeout the request
  fails with **503** `queue_timeout` + `Retry-After` naming the trigger. Keep it
  below your clients' own request timeout.

### Overload triggers

Each trigger decides independently whether a request waits. Concurrency and
capacity are on by default; the rest are opt-in and the telemetry-based ones are
inert unless `BROKER_GPU_TELEMETRY` is on.

| Trigger | Setting | Fires when |
|---|---|---|
| Concurrency | `BROKER_QUEUE_TRIGGER_CONCURRENCY` (default on) | global or per-model concurrency full |
| Capacity | `BROKER_QUEUE_TRIGGER_CAPACITY` (default on) | `max_loaded_models` / memory-aware admission can't fit now but a holder may go idle |
| Device memory | `BROKER_QUEUE_TRIGGER_DEVICE_MEMORY` + `BROKER_QUEUE_DEVICE_FREE_MIB_MIN` / `_USED_FRACTION_MAX` | measured free MiB below / used fraction above the threshold (telemetry on) |
| Host | `BROKER_QUEUE_TRIGGER_HOST` + `BROKER_QUEUE_HOST_LOAD_PER_CPU_MAX` | 1-min load average per CPU over the threshold |
| Backend | `BROKER_QUEUE_TRIGGER_BACKEND` + `BROKER_QUEUE_BACKEND_WAITING_MAX` | vLLM `num_requests_waiting` over the threshold, or an upstream 429/Retry-After window (ADR 0017) |

A capacity error that can **never** fit (`exceeds_device_capacity`) is never
queued — it fails fast with a 429 naming the holder.

## Fairness and priority

- **FIFO within one API key**, **round-robin across keys** — one key flooding the
  queue cannot starve another; each key takes turns.
- **Priority** (keys file, ADR 0016): set `limits.priority` on a key (default 0);
  higher is served first under overload. **Aging**
  (`BROKER_QUEUE_PRIORITY_AGING_SECONDS`, default 10) raises a waiter's effective
  priority the longer it waits, so low-priority requests never starve.

```yaml
# keys.yaml
- id: 1a2b3c4d
  limits:
    priority: 10     # served ahead of default (0) under overload
```

## Reading the headers and /status

Non-streaming responses that waited carry:

```
x-broker-queue-wait-ms: 212
x-broker-queue-trigger: model_concurrency
```

`GET /status` has a `queue` block:

```json
"queue": {
  "enabled": true, "depth": 7, "max_depth": 128, "max_wait_seconds": 30,
  "per_model_depth": {"chat": 7}, "per_key_depth": {"brk_1a2b": 5, "brk_9f8e": 2},
  "oldest_wait_ms": 1840.2,
  "waiting_triggers": {"model_concurrency": 7},
  "enqueued_total": 412, "admitted_total": 398,
  "rejections_by_reason": {"model_concurrency": 9},
  "timeouts_by_reason": {"host_load": 5}
}
```

Prometheus series (on `/metrics`, exposure unchanged): `broker_queue_enqueued_total`
(by trigger), `broker_queue_admitted_total`, `broker_queue_rejected_total` (by
trigger), `broker_queue_timeouts_total` (by trigger), `broker_queue_depth`,
`broker_queue_wait_seconds` (histogram). With tracing on, the `inference` span
carries `broker.queue.wait_ms` / `broker.queue.trigger`.

## Interplay with governance 429s

Per-key **quota** limits (rpm/tpm/budgets, ADR 0008/0016) are enforced *before*
the queue and are **never** queued — a quota 429 (`rate_limit_exceeded` /
`insufficient_quota`) is the caller's own limit and retrying/waiting won't change
it. Only *server* overload is queued. So a `429 rate_limit_exceeded` is a quota
verdict; a `429 queue_full` means the admission queue itself is full; a
`503 queue_timeout` means the server stayed overloaded for your whole wait.

## Streaming

The queue wait happens strictly **before the first byte**. A streaming request
either starts normally (after any wait) or, if the queue is full / the wait times
out, returns a normal JSON error in the SSE stream — a half-emitted stream is
never retried or queued (ADR 0017). Because SSE response headers are flushed
before the wait, a streaming request reports its wait via `/status` and the OTel
span rather than a response header; keep client/proxy keep-alive timeouts above
`BROKER_QUEUE_MAX_WAIT_SECONDS` so a long wait isn't cut off.

## Multi-replica (Redis)

Each replica runs its **own local queue** — depth and fairness are per process
(as with Envoy's circuit-breaker counters). Size `MAX_DEPTH` per replica and
remember the fleet's effective queue is the sum across replicas. The Redis
StateStore (ADR 0009) shares routing/cooldown state, not the admission queue.

## Composition summary

- Composes with the existing global + per-model semaphores (no double-limiting).
- Retries/failover: a queue-full/timeout verdict is terminal (never fails over and
  re-queues on another member); a load error still fails over as before.
- Cache hits are served in the API layer and **bypass** the queue.
- `auto` resolves the model **before** queueing and never switches model under
  overload (no silent cross-model fallback).
- Client disconnect removes the waiter immediately (no leaked slot).

See [ADR 0023](../adr/0023-request-queueing.md), the
[research note](../research/adr-0023-request-queueing.md), the tested
[example](../../examples/request-queueing/), and the simulated benchmark
`scripts/bench_queue.py`.
