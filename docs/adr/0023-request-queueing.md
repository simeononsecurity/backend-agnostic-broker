# ADR 0023: Bounded, fair request queueing under overload

Status: Accepted
Date: 2026-10-20
Supersedes: —
Related: ADR 0020 (resource telemetry + measured release), ADR 0022 (memory-aware
admission), ADR 0017 (reliability v2: retries/cooldowns, upstream 429/Retry-After),
ADR 0008/0016 (per-key governance), ADR 0003 (pools/sticky/health),
`docs/research/adr-0023-request-queueing.md`

## Context

The user's goal: *"if we detect a given system or GPUs are overloaded, we can
queue requests"* instead of failing them.

In v0.6.0 a request that cannot be served immediately either **blocks a
concurrency semaphore unboundedly** (`load_concurrency_policy="queue"`, the
default — no bound, timeout, priority or fairness) or **429s immediately**
(`"reject"`). Memory-aware admission (ADR 0022) and `max_loaded_models` raise a
capacity 429 the moment they can't fit, even when an in-flight model is about to
go idle. There is no way to *hold* a request through a transient overload, no
fairness between API keys (one key's flood sits ahead of everyone else on the
semaphore), and no visibility into waiting.

ADR 0020 added opt-in measured GPU/host telemetry and ADR 0022 a typed
`CapacityUnavailableError`. This ADR builds an admission queue on top of them.

## Decision

Add an **opt-in admission queue** (`BROKER_QUEUE_ENABLED`, default OFF ⇒ behavior
== v0.6.0) in front of model serving, implemented as `broker/admission.py`
(`AdmissionQueue` scheduler) + the admission methods in `broker/manager.py`. A
request that cannot be admitted *right now* waits in the queue instead of blocking
a semaphore or failing.

1. **Triggers (each individually configurable; telemetry-based ones inert when
   telemetry is off).** A request enters the queue when it cannot be admitted
   because of: global or per-model **concurrency** saturation; **capacity** — no
   evictable model (`max_loaded_models`) or memory-aware admission (ADR 0022) says
   it can't fit now but a holder may go idle; **device memory** pressure (ADR 0020
   measured free MiB / used fraction); **host** overload (1-min load average per
   CPU via `os.getloadavg`, cross-platform, cheap, cached); or **backend
   backpressure** (vLLM `num_requests_waiting` via `collect_load`, or an upstream
   429/Retry-After window from ADR 0017). A capacity error that can *never* fit
   (`exceeds_device_capacity`) is **not** queued — it fails fast.

2. **Bounded + timed.** `BROKER_QUEUE_MAX_DEPTH` (global) and
   `_PER_MODEL` bound the waiting set; a new request over either bound is rejected
   with **429** `queue_full` + `Retry-After` and never enqueued.
   `BROKER_QUEUE_MAX_WAIT_SECONDS` bounds the wait; on timeout the request fails
   with **503** `queue_timeout` + `Retry-After` naming the trigger. A client
   disconnect (task cancellation) removes the waiter immediately — no leaked slot.

3. **Fairness + priority.** FIFO within one API key; **round-robin across keys**
   (a per-key "last admitted" pointer) so one key cannot starve another; an
   optional per-key `priority` (keys file, ADR 0016) orders waiters, with
   **aging** (`BROKER_QUEUE_PRIORITY_AGING_SECONDS`) so a low-priority request's
   effective priority rises over time and never starves. This is a simplified
   Deficit-Round-Robin / FQ-CoDel scheme (see the research note).

4. **Composition, no double-limiting, no deadlock.** The queue sits in front of —
   and still uses — the existing global + per-model semaphores: a waiter acquires
   them only when it is its turn and no overload is active. It composes with
   retries/failover (a queue-full/timeout verdict is classified terminal, so it
   never fails over and re-queues on another member), cache hits (served in the
   API layer before the manager, so they bypass the queue), the auto-router (the
   route is resolved **before** queueing — the queue waits on the chosen model and
   **never** silently switches model), and streaming (the wait happens strictly
   **before the first byte**; ADR 0017's "never queue a mid-stream request" holds).
   Redis multi-replica: **each replica queues locally** (documented; Envoy's
   circuit-breaker counters are per-process too).

5. **Visibility.** Response header `x-broker-queue-wait-ms` (+ `-queue-trigger`)
   on non-streaming replies; a `/status` `queue` block (depth per model and key,
   oldest wait, active triggers, rejections and timeouts by reason); new Prometheus
   series (`broker_queue_*`; `/metrics` exposure itself unchanged); and an OTel
   span attribute (`broker.queue.wait_ms`) when tracing is on.

**Quota 429s are never queued.** Per-key rpm/tpm/budget limits (ADR 0008/0016) are
enforced in the API layer before the manager and are a *user* limit, not a service
overload; `QueueFull`/`QueueTimeout` are classified terminal and a quota error is
never passed to the queue.

## Alternatives considered

- **Wait-timeout → 429 or 408 instead of 503.** Rejected: the service was
  overloaded for the whole wait regardless of the caller — a *service* limit, which
  is 503 per Azure Well-Architected; matches vLLM's 503 queue rejection and Envoy's
  503 pending-overflow. 408 (PTU-style queue timeout) is less widely understood by
  OpenAI-compatible clients. (Queue-**full** stays 429 per the card: a new arrival
  is contributing to overload and should back off.)
- **Full weighted fair queuing (WFQ).** Rejected: O(log n) and more state than a
  single-replica admission gate needs; DRR gives the same fairness at O(1).
- **Preempting in-flight/running requests** (vLLM-style preemption). Rejected:
  punishes work already done and would break the streaming invariant; the broker
  only queues *before* admission.
- **A shared cross-replica queue** (Redis-coordinated). Rejected: a distributed
  lock on the hot path; local per-replica queueing is the accepted model (Envoy).
- **Replacing the semaphores with the queue.** Rejected: composing with them keeps
  the change additive and avoids double-counting concurrency.
- **An opt-in `router.overflow` cross-model alternative.** Rejected for this ADR:
  it risks a silent cross-model fallback; the no-silent-switch invariant wins. Can
  be revisited as an explicit, documented feature later.

## Consequences

- Operators can turn overload failures into bounded waits, tuning depth/wait and
  which signals trigger queueing, without changing default behavior.
- One more place that must stay cancellation-safe; the admission loop dequeues in a
  `finally` and releases any reserved slot, and a woken-but-blocked waiter re-queues
  without busy-spinning (re-woken by slot releases or the background pump).
- The queue is per-replica; a fleet's effective queue is the sum of replicas'.
- Telemetry/host/backend triggers add a cheap, cached probe on the admission path
  only when enabled.

## Confirmation

`tests/test_queue.py` (14 tests): saturation → queued then served **FIFO**;
**fairness** (key A floods, key B still served within bound, round-robin);
**priority** ordering + an **aging** unit test; **max depth → 429 + Retry-After**;
**max wait → 503 + Retry-After** naming the trigger; **client disconnect frees the
slot**; **quota 429s and `exceeds_device_capacity` never queued**; **fast path
bypasses the queue**; **device-telemetry overload queues then drains**; **host-load
overload queues then drains**; **composes with failover (pool, one bad member) with
no deadlock**; **the queue never switches model**; and **queue OFF == v0.6.0**. The
labeled load benchmark is `scripts/bench_queue.py`. The v0.6.0 test suite passes
unchanged against this branch (default OFF).
