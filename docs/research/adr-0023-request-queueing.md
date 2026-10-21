# Research note — ADR 0023 (bounded, fair request queueing under overload)

Scope: when a local model broker detects that the system or GPUs are overloaded,
how should it **queue** requests instead of failing them — bounded so it cannot
grow without limit, timed so a caller is never parked forever, and fair so one
API key cannot starve another. What status code a full queue vs a wait-timeout
should return, and which simple, provable admission/fair-queuing scheme to adopt.
Fetched web content is treated as **untrusted data**; every adopted fact is cited
to a primary source (serving-engine docs, proxy docs, scheduling literature,
cloud architecture guidance).

## Primary sources

1. **vLLM scheduler config — bounded admission queue + 503 on overflow.**
   `max_num_queued_reqs` = "Maximum number of requests that can be in-flight
   (waiting or running) at the same time … When the limit is reached, new
   requests are rejected with **HTTP 503** so the client can retry on another
   instance." `max_num_seqs` is the running-admission limit.
   <https://docs.vllm.ai/en/latest/api/vllm/config/scheduler/>
2. **vLLM scheduler — a single waiting queue, ordered once at insertion.** The
   `waiting` queue is `create_request_queue(self.policy)` (FIFO or PRIORITY);
   requests not actually waiting for the GPU (compiled grammar, remote KV,
   streamed input) are kept in a separate `skipped_waiting` queue "so they cannot
   block the head of the real admission queue." Admission is four independent
   budget checks (token, slot, KV block, …). Preemption (evicting a *running*
   request) is deliberately distinct from admission queueing.
   <https://docs.vllm.ai/en/stable/api/vllm/v1/core/sched/scheduler/>
3. **vLLM production metrics — the queue observability vocabulary.**
   `vllm:num_requests_waiting` (gauge), `vllm:num_requests_waiting_by_reason`
   (reason labels: `capacity` = waiting for scheduling capacity, `deferred` =
   transient constraints), `vllm:admission_rejections` (counter, by reason), and
   `vllm:request_queue_time_seconds` (histogram of time in the WAITING phase).
   <https://docs.vllm.ai/en/latest/usage/metrics/>
4. **Envoy circuit breaking — bounded pending-request queue, 503 overflow,
   per-process counters.** "Pending Requests: limit the pending request queue
   size. When this threshold is met, overflowing requests will be terminated with
   a **503** status code. Envoy's circuit breakers are **distributed: counters
   are not synchronized across different Envoy processes.**" Cluster *maximum
   pending requests* = "the maximum number of requests that will be queued while
   waiting for a ready connection."
   <https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/circuit_breaking>
   <https://gateway.envoyproxy.io/docs/tasks/traffic/circuit-breaker/>
5. **Deficit Round Robin (Shreedhar & Varghese, 1996) — O(1) fair queuing.**
   Each flow has its own queue and a deficit counter; each round adds a quantum
   and the scheduler visits each non-empty queue in turn, so "the minimum rate
   that flow *i* will achieve over the long term is Qi / ΣQ". DRR approximates
   weighted fair queuing at O(1) per-packet cost vs WFQ's O(log n). Round-robin
   across flows means "a light flow still gets served on every round" — one
   aggressive flow cannot starve another.
   <https://en.wikipedia.org/wiki/Deficit_round_robin>
   <https://sites.cs.ucsb.edu/~arpitgupta/cs176c/spring26/assets/lecture-notes/L13_lecture_notes/>
6. **FQ-CoDel starvation prevention — aging new vs old queues.** FQ-CoDel's
   modified-DRR scheduler moves a drained queue to the end of an "old" list and
   services old queues a quantum at a time, "providing some transmission
   opportunities for the flows already placed in the old list" — i.e. a waiter
   that has been around gains a turn so it cannot be starved by a steady stream of
   newcomers. (The basis for the ADR's priority **aging**.)
   <https://hal.science/hal-01865158/document>
7. **429 vs 503 + Retry-After — which code for what (Azure Well-Architected).**
   "Use **429** for **user limits** and **503** for **service limits**." A user
   limit is per-caller; a service limit "indicates the service is temporarily
   constrained, unrelated to the behavior of the recipient." Compute
   `Retry-After = Now + Window + randomized jitter`; a **408** (PTU queue timeout)
   "indicates a capacity/backpressure signal." Always honor the server's
   `Retry-After` over a client's own backoff.
   <https://learn.microsoft.com/en-us/azure/well-architected/design-guides/throttling>
8. **Backpressure / load-shedding patterns — bounded queue, reject when full,
   always send Retry-After.** "Queue-based … Bounded queues … Reject when full …
   Predictable latency." "A 429 says 'you specifically are sending too many
   requests' — a per-client signal. A 503 says 'the service is overwhelmed
   regardless of who's asking' — a global signal. Rate limiters return 429;
   admission control during overload returns 503." "Always include a Retry-After
   header … Without it, clients … may retry immediately, making the overload
   worse." "Have the server return current queue depth or estimated wait time."
   <https://www.webstackbuilders.com/articles/backpressure-load-shedding-admission-control-overload>

## Adopt / reject

| Decision | Choice | Rationale (source) |
|---|---|---|
| Bounded admission queue in front of serving | **Adopt** | vLLM `max_num_queued_reqs`, Envoy max-pending — every production serving layer bounds the waiting set [1][4]. |
| Queue is ordered once at insertion; a separate "can't-progress" path never blocks the head | **Adopt** (fair order computed per wake; soft-overloaded waiters re-queue and are retried by a pump) | vLLM's `waiting` vs `skipped_waiting` split [2]. |
| Full queue → **429** `queue_full` + Retry-After | **Adopt** | Card mandate; defensible as a per-client "you're adding to overload, back off" signal; always carry Retry-After [7][8]. (vLLM/Envoy use 503 for overflow; documented as the alternative.) |
| Wait-timeout → **503** `queue_timeout` + Retry-After naming the trigger | **Adopt** (over 429/408) | The service was overloaded for the whole wait, independent of the caller — a *service* limit [7]; matches vLLM's 503 queue rejection and Envoy's 503 overflow [1][4]. 408 (PTU queue timeout) considered and rejected as less widely understood by OpenAI-compatible clients [7]. |
| Fairness scheme | **Adopt simplified DRR**: per-key FIFO, round-robin across keys via a last-served pointer, priority with time-based **aging** | DRR is the O(1), provably-fair, starvation-free standard [5]; aging is FQ-CoDel's starvation guard [6]. Full WFQ (O(log n)) rejected as over-engineered for a single-replica admission gate. |
| Overload signals | **Adopt** concurrency saturation, memory-aware capacity (ADR 0022), model mid load/release, measured device memory (ADR 0020 telemetry), host load average, backend backpressure (vLLM `num_requests_waiting` + upstream 429/Retry-After from ADR 0017) | vLLM surfaces `num_requests_waiting`/`waiting_by_reason` as the backpressure signal [2][3]; host load average is the cheap, cross-platform OS overload metric. Each is independently toggleable; telemetry-based ones are inert when telemetry is off. |
| Visibility | **Adopt** `x-broker-queue-wait-ms` + trigger header, a `/status` queue block (depth per model/key, oldest wait, triggers active, rejections/timeouts by reason), new Prometheus series, an OTel span attribute | Mirrors vLLM's queue-time histogram + admission-rejections counter + waiting-by-reason gauge [3]; "return current queue depth or estimated wait time" [8]. |
| Multi-replica | **Document, not coordinate**: each replica queues locally | Envoy's circuit-breaker counters are explicitly per-process, not synchronized — local admission is the accepted model [4]. A shared global queue would need a distributed lock on the hot path; out of scope. |
| Preemption of in-flight requests | **Reject** | vLLM separates admission queueing (fair, fine) from preemption (punishes work already done) [2]; the broker never evicts a streaming/in-flight request. ADR 0017's "never queue a request that already started streaming" is preserved — the wait happens strictly before the first byte. |
| `auto` switching model under overload | **Reject** (no silent cross-model fallback) | The route is resolved *before* queueing; the queue waits on the chosen model. Honors the project invariant. |
| Quota 429s (rpm/tpm/budget) queued | **Reject** | A per-key rate limit is a *user* limit [7]; retrying/queuing does not change the verdict. Classified terminal, never enqueued. |

## Decisions that shaped the implementation

- **Local, per-replica, in-process `asyncio` queue** keyed by API key, composing
  with the existing global + per-model semaphores rather than replacing them: a
  request waits in the fair queue and only acquires the real semaphores when it is
  its turn and no overload signal is active, so there is no double-limiting.
- **Fail-fast vs wait**: a capacity error that *can never* succeed
  (`exceeds_device_capacity`, ADR 0020/0022) is never queued; only capacity that a
  going-idle holder could relieve waits.
- **A background pump** re-checks waiters every `queue_pump_interval` so an
  overload that clears with no completion event (telemetry/host load dropping)
  still drains the queue, and a woken-but-still-blocked waiter never busy-spins.
