# Research: cache-aware routing v2 — a queueing model for the bench, re-validated β, and the ADR 0010 deferrals

Prior-art note backing **ADR 0018** (extends ADR 0010). Confidence: **High** for the
queueing model (the design is a direct port of published LLM-serving simulators that
validate against real backends to <10 % error) and for the ADR 0010 deferral
decisions (re-confirmed against the same primary sources). Confidence **Medium** on
the *absolute* latency numbers the bench prints — it is a deliberately simplified,
seeded simulation, not a measurement of a real GPU.

> **Untrusted sources.** Everything below was fetched from public docs/papers and is
> cited as *data*. None of it issued instructions to this agent; no page contained
> agent-directed text. Live version numbers, defaults, and formulas were read on
> 2026-10-19 and may drift; re-verify before quoting.

## Why this note exists

ADR 0010 shipped `cache_aware` routing and a benchmark whose simulated TTFT was a
pure routing-savings model: `TTFT = TTFT_BASE − hit_fraction × TTFT_SAVED`, with **no
queueing delay from load imbalance**. That made the published TTFT/p95 wins an
*upper bound*: the hit-rate and load-spread results were routing-only and robust, but
nothing penalized a strategy that piled work onto one hot member. ADR 0010 chose the
default `β = 0.25` from a balance bound on load spread *because* the harness could not
price imbalance as latency. The headline ADR 0018 task is to add a queueing model so
load imbalance produces real queueing delay, then re-validate β against it.

## Sub-questions and findings

### 1. How is LLM-serving latency modeled as a queueing system? (the service model)

Published LLM-serving simulators converge on the same structure: **each engine /
GPU pool is an M/G/c (or M/M/c) queue** — Poisson arrivals at rate λ, c parallel
servers (the continuous-batching concurrency), general service time — and TTFT
decomposes into **queue wait + prefill time**.

`inference-fleet-sim` ([arXiv 2603.16054][ifs]) is the closest match and the model we
port. It states each GPU pool is "modeled as an M/G/c queue: Poisson arrivals at rate
λ, general service time with mean E[S] … and c parallel servers", and gives:

- **Continuous-batching iteration latency grows with the batch size**:
  `t_iter(n) = W + H · n` — a baseline compute term `W` plus a per-concurrent-sequence
  term `H · n`. This is the key fact that turns load imbalance into latency: a member
  running more concurrent sequences runs every one of them slower.
- **Service time** of a request with `Lin` input / `Lout` output tokens:
  `E[S] = (⌈Lin / Cchunk⌉ + Lout) / nmax · t_iter(nmax)` — prefill is `⌈Lin/Cchunk⌉`
  chunked-prefill iterations, decode is `Lout` iterations.
- **TTFT decomposition**: `TTFT = Wqueue + ⌈Lin/Cchunk⌉ · t_iter(nmax) + t_iter(nmax)`
  — i.e. **queue wait + prefill**. "For large requests … Tprefill alone can eat most
  of the SLO budget even when there is no queue wait at all."
- **The simulator is a discrete-event sim with a per-instance min-heap event queue**;
  "the DES replays the actual arrival sequence with realistic service time draws" and
  reports per-request queue wait, TTFT, and end-to-end latency with a **P99 TTFT**
  SLO check. At ρ ≈ 0.30 it already measures P99 TTFT ≈ 1 s because "long-tail service
  events create cascading back-pressure" — load imbalance and tails matter.

`BestServe` ([arXiv 2506.05871][bestserve]) independently builds "a queueing system …
[that] models the arrival patterns of requests and performs temporal simulations to
compute statistics such as arrival times, queueing delays, processing times, and
departure times", advancing a time counter and batching all requests that have
arrived, to report TTFT/TPOT percentiles vs SLOs.

`Carbon-SLO` ([arXiv 2610.14571][carbon]) confirms the decomposition and that
**queueing delay is occupancy-dependent, not a constant**: "the queueing delay
depends on the engine's occupancy rather than being a fixed constant… A request's
TTFT thus includes the scheduling delay, any inter-region transfer, queueing time,
and prefill duration." Their event-driven simulator reproduces real TTFT "ranging
from 71 ms to over 20 s as queueing builds up, with an overall average error of
9.1 %" — evidence that this class of model is faithful enough to re-validate β on.

**Adopt:** a seeded **discrete-event simulation**, Poisson arrivals, each pool member
a continuous-batching server with `nmax` concurrent slots and iteration cost
`t_iter(n) = W + H·n`; per request `prefill_iters = ⌈uncached_prefix_tokens/Cchunk⌉`
and `decode_iters = Lout`; **TTFT = queue-wait + prefill time**; report TTFT
p50/p95/p99, throughput, and per-member utilization. Cache hits shrink
`uncached_prefix_tokens` (prefill savings) exactly as in the physical KV cache.

### 2. Prefill vs decode: what scales with what?

vLLM's own docs and anatomy post establish the two phases: **prefill** is "a forward
pass over all prompt tokens … usually compute-bound"; **decode** is "a forward pass
over just the most recent token … memory-bandwidth-bound" ([vLLM anatomy][anatomy]).
Chunked prefill "chunk[s] large prefills into smaller chunks and batch[es] them
together with decode requests", tunable via `max_num_batched_tokens` /
`long_prefill_token_threshold`; it trades TTFT for ITL ([vLLM optimization][vllm-opt]).
A KV-cache characterization study confirms the clean separation empirically:
"Prefill time remains constant across output lengths … Decode time, however, scales
linearly with output length" ([arXiv 2604.05012][kvchar]). AWS notes the load effect
we must model: "continuous batching improves overall throughput but **increases
time-to-first-token (TTFT) for high-concurrency workloads**" ([AWS vLLM][aws]).

**Adopt:** prefill time ∝ **uncached** prefix tokens (so a prefix cache hit is a
prefill-time saving, the whole point of `cache_aware`); decode time ∝ output tokens;
both inflated by batch-size interference via `t_iter(n)=W+H·n`. This is why a
load-imbalanced strategy now pays: a hot member batches more sequences, slowing every
sequence's prefill and decode and lengthening its queue.

### 3. Sanity-checking the simulation (Erlang-C / M/M/c / Little's law)

An M/M/c queue has closed-form steady-state results we use as **sanity oracles** for
the DES. The Erlang-C formula gives P(wait) = `C(c, a)` with offered load `a = λ/μ`
and ρ = a/c; the mean wait in queue is `Wq = C(c,ρ)/(c·μ·(1−ρ))`, and Little's law
ties it together: `L = λ·W`, `W = Wq + 1/μ`
([MetricGate M/M/c guide][mmc]; [MIT 1.041 L8][mit]; Erlang delay formula [PSG][psg]).

**Adopt (as tests, not as the model):** under **uniform** load with `β` large enough
to spread evenly, the four members' queues must be **symmetric** (equal within noise);
**throughput** must satisfy Little's law (completed = arrivals at steady state, mean
in-system ≈ λ·W); and **zero cache** ⇒ `cache_aware ≈ least_busy` (no affinity signal
to trade against load). These are the model-correctness tests the card requires.

### 4. Does a queue-aware / output-length-aware load signal help? (deferral #3)

The literature is clear that **length-/work-aware scheduling helps a lot** when you
can estimate remaining work: `Block` ([arXiv 2508.03611][block]) predicts response
length and simulates to schedule, cutting "average request latency by 19.9–45.8 % and
P99 tail latency by 12.6–49.5 %", with TTFT gains even larger. llm-d's load scorers
key off real `vllm:num_requests_waiting` / `kv_cache_usage_perc` rather than a raw
request count ([llm-d optimized baseline][llmd], carried over from ADR 0010). **But**
the big wins require either (a) an output-length *predictor* (an extra model — Block
trains one) or (b) the engine's own queue metric. The broker owns **no tokenizer and
no length predictor** (ADR 0010 invariant) and must stay backend-agnostic.

The dependency-free middle path the broker *can* do is a **prefill-work proxy**: it
already holds each in-flight request's prompt, so it can weight load by *in-flight
prompt characters* (a proxy for queued prefill work) instead of a flat request count —
no tokenizer, no predictor, no backend coupling. **Decision gated on the bench:** the
queueing bench compares `load_signal = count` vs a `work` oracle (remaining iters) vs
the implementable `chars` proxy; §Decision in the ADR records the measured outcome and
whether the proxy is adopted (opt-in, default off) or rejected with the data. The
real engine-metric path already exists as `BROKER_CACHE_AWARE_USE_METRICS` (ADR 0010).

### 5. Cross-replica LRU size bound under Redis (deferral #4)

ADR 0010's prefix index is size-bounded by a **per-replica** in-process LRU
(`OrderedDict`); under Redis the TTL is the only *cross-replica* bound, so N replicas
can hold up to N× the intended entries. Redis supports a bounded-LRU pattern natively
with a **sorted set**: score each index key by last-access time (`ZADD`), and trim the
oldest by rank (`ZREMRANGEBYRANK`) when `ZCARD` exceeds the cap — the standard
Redis "capped sorted set / LRU" idiom ([Redis ZADD][zadd]; [Redis ZREMRANGEBYRANK][zrr]).
The broker's `RedisStateStore` already uses exactly this ZSET vocabulary for its
sliding-window rate counters (`zadd`/`zremrangebyscore`/`zcard`), so the surface
exists. **Decision: implement** a generic sorted-set LRU on the `StateStore` (both
backends) and have the prefix index trim through it, giving a true cross-replica size
bound; tested with `fakeredis`. (Alternative considered: Redis key eviction policy —
rejected, it is a server-wide config the broker cannot assume and would evict
unrelated keys.)

### 6. Token-ID block hashing, precise KV-event (ZMQ) index, Preble autoscaling (deferrals #5, #6)

Re-confirmed against the ADR 0010 sources; nothing has changed the tradeoff:

- **Token-ID block hashing** (vLLM APC, AIBrix) is more precise but **needs a
  per-model tokenizer the broker deliberately does not own**. A dependency-free path
  would mean bundling tokenizers per backend model — exactly the coupling ADR 0010
  avoids. **Reject** (character blocks stay; SGLang precedent: raw text).
- **Precise KV-event (ZMQ) index** (llm-d / AIBrix KV-sync) subscribes to each
  engine's `BlockStored`/`BlockRemoved` events for highest fidelity, but **couples the
  broker to vLLM/SGLang internals and needs every pod wired for event streaming** —
  violating the backend-agnostic invariant. **Reject** (out of scope / coupling).
- **Preble-style prefix autoscaling** (replicate a hot prefix across GPUs) is a
  *scheduler/placement* feature, not a router concern, and the broker does not own
  model placement/replication. **Reject** (out of scope).

These are recorded as **Rejected with rationale** in ADR 0018 (not "deferred"), per
the worker brief's no-leftovers rule.

## Adopt / Reject summary

| Item | Decision | Basis |
|---|---|---|
| Discrete-event queueing model (M/G/c, continuous batching `t_iter=W+H·n`, TTFT=wait+prefill, p50/p95/p99 + throughput + utilization) | **Adopt** | inference-fleet-sim, BestServe, Carbon-SLO |
| Prefill ∝ uncached prefix tokens; decode ∝ output tokens; batch-size interference | **Adopt** | vLLM anatomy/docs; KV-char study |
| Re-validate default β under queueing via an explicit rule (minimize fleet p95 TTFT s.t. no scenario worse than sticky by >X% and long_lived spread ≤1.25×) | **Adopt** | this note; ADR 0018 §Decision records the chosen β |
| Erlang-C / Little's-law sanity tests (uniform⇒symmetric; zero-cache⇒≈least_busy; Little's law throughput) | **Adopt (tests)** | M/M/c closed forms |
| Queue-/work-aware load signal | **Decide from bench** (chars proxy if it helps, else Reject) | Block; llm-d; ADR 0010 no-tokenizer invariant |
| Cross-replica Redis sorted-set LRU bound for the prefix index | **Adopt (implement)** | Redis ZADD/ZREMRANGEBYRANK; existing ZSET use |
| Token-ID block hashing | **Reject** | needs per-model tokenizer |
| Precise KV-event (ZMQ) index | **Reject** | couples to engine internals |
| Preble-style prefix autoscaling | **Reject** | scheduler/placement, out of router scope |

## Sources

1. [inference-fleet-sim: A Queueing-Theory-Grounded Fleet Simulator (arXiv 2603.16054)][ifs] — M/G/c + DES, `t_iter(n)=W+H·n`, `E[S]`, TTFT decomposition, min-heap per-instance DES, P99 SLO, Kimura two-moment approx.
2. [BestServe: Serving Strategies with Optimal Goodput (arXiv 2506.05871)][bestserve] — queueing simulator of arrival/queueing/processing/departure; TTFT/TPOT percentiles vs SLO.
3. [Dissecting the Carbon-SLO Tension in Geo-Distributed LLM Serving (arXiv 2610.14571)][carbon] — occupancy-dependent queueing delay; TTFT = scheduling+queue+prefill; event-driven sim validated to <10 % error.
4. [Block: Balancing Load in LLM Serving with Context, Knowledge and Prediction (arXiv 2508.03611)][block] — length prediction + simulation scheduling; P99 TTFT SLO (Vidur capacity def); length-aware latency gains.
5. [The Anatomy of vLLM (vLLM blog, 2025-09-05)][anatomy] — prefill compute-bound / decode memory-bound; chunked prefill `long_prefill_token_threshold`.
6. [vLLM — Optimization and Tuning (chunked prefill)][vllm-opt] — `max_num_batched_tokens`, decode-prioritized scheduling, TTFT/ITL tradeoff.
7. [What is vLLM? (AWS)][aws] — continuous batching raises TTFT under high concurrency; prefill blocks decode; disaggregation.
8. [Comparative Characterization of KV Cache Management Strategies (arXiv 2604.05012)][kvchar] — prefill time constant across output length; decode scales linearly with output length.
9. [M/M/1 vs M/M/c Queueing Models (MetricGate)][mmc], [MIT 1.041 Lecture 8][mit], [Erlang delay formula (PSG)][psg] — Erlang-C `C(c,a)`, `Wq=C(c,ρ)/(cμ(1−ρ))`, Little's law.
10. [Redis ZADD][zadd] / [Redis ZREMRANGEBYRANK][zrr] — sorted-set capped-LRU idiom for the cross-replica index bound.
11. [llm-d — Deploy an Optimized Baseline][llmd] — real-metric load scorers (carried from ADR 0010).

[ifs]: https://arxiv.org/pdf/2603.16054v1
[bestserve]: https://arxiv.org/html/2506.05871v1
[carbon]: https://arxiv.org/pdf/2610.14571
[block]: https://arxiv.org/html/2508.03611v2
[anatomy]: https://vllm.ai/blog/2025-09-05-anatomy-of-vllm
[vllm-opt]: https://docs.vllm.ai/en/stable/configuration/optimization/
[aws]: https://aws.amazon.com/what-is/vllm/
[kvchar]: https://arxiv.org/html/2604.05012v1
[mmc]: https://metricgate.com/blogs/queueing-theory-mm1-mmc-models/
[mit]: https://web.mit.edu/1.041/www/lectures/L8-queuing-models-2026sp.pdf
[psg]: http://elearn.psgcas.ac.in/nptel/courses/video/111103159/lec15.pdf
[zadd]: https://redis.io/docs/latest/commands/zadd/
[zrr]: https://redis.io/docs/latest/commands/zremrangebyrank/
[llmd]: https://llm-d.ai/docs/dev/well-lit-paths/foundations/optimized-baseline
