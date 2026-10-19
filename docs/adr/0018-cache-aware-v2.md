# ADR 0018: Cache-aware routing v2 — a queueing bench, re-validated β, and the ADR 0010 deferrals

- Status: Accepted
- Date: 2026-10-19
- Supersedes: none (extends ADR 0010 cache-aware pool routing; builds on ADR 0009 StateStore)
- Related: ADR 0010; `docs/routing-and-health.md`; `docs/roadmap.md` item 6;
  `docs/research/adr-0018-cache-aware-v2.md` (cited prior art); prior art: vLLM
  production-stack, llm-d, SGLang, AIBrix, Preble, inference-fleet-sim
  (arXiv 2603.16054), BestServe (arXiv 2506.05871), Carbon-SLO (arXiv 2610.14571)

## Context

ADR 0010 added opt-in `cache_aware` pool routing (prefix affinity + load-aware
scoring) and a seeded benchmark, `scripts/bench_routing.py`. That benchmark modeled
latency as **pure routing savings** — `TTFT = TTFT_BASE − hit_fraction × TTFT_SAVED` —
with **no queueing delay from load imbalance**. ADR 0010 was explicit that this made
its TTFT/p95 numbers an *upper bound*: the hit-rate and load-spread results were
routing-only and robust, but nothing in the model penalized a strategy that piled
work onto one hot member. The default `β = 0.25` was therefore chosen from a
*balance-only* heuristic ("smallest swept β whose low-pressure load spread stays
within 25%"), not from a latency measurement.

ADR 0010 also left a set of **deferred items** (precise KV-event index, token-ID
hashing, cross-replica LRU bound, output-length-aware load, Preble autoscaling). The
worker brief for this repo requires every deferred item to end as *implemented +
tested* or *explicitly Rejected with rationale* — never "deferred".

This ADR (a) adds a **queueing model** to the bench so load imbalance produces real
latency, (b) **re-validates the default β** against it with an explicit selection
rule, and (c) **resolves every ADR 0010 deferral**.

The research note (`docs/research/adr-0018-cache-aware-v2.md`, ≥11 cited sources)
shows that published LLM-serving simulators converge on one structure: each engine is
an M/G/c queue (Poisson arrivals, c continuous-batching slots), continuous-batching
iteration latency grows with batch size as `t_iter(n) = W + H·n`, and **TTFT = queue
wait + prefill time**, where prefill scales with *uncached* prompt tokens and decode
with output tokens. These simulators validate against real backends to <10% error.

## Decision

### 1. Queueing model in the benchmark (headline — implemented)

`scripts/bench_routing.py` now runs a **discrete-event simulation** of the pool
instead of the routing-savings formula. Routing stays real — every arrival injects
the current per-member load into the manager and the real `_select_member` /
`_select_cache_aware` picks the target; only *service* is simulated:

- Each member is a continuous-batching server with `member_slots` (`nmax`) concurrent
  slots; one engine step advances every batched sequence by one iteration and costs
  `t_iter(n) = iter_base_ms + iter_per_seq_ms · n`.
- A request needs `prefill_iters = ⌈uncached_prefix_tokens / prefill_chunk_tokens⌉`
  then `decode_iters = output_tokens`; a prefix **cache hit shrinks
  uncached_prefix_tokens**, so cache locality reduces prefill time (and frees slots
  sooner). Arrivals are a seeded Poisson process; output lengths are sampled.
- **TTFT = queue wait + prefill time.** The bench reports TTFT **p50/p95/p99 incl.
  queueing**, **throughput**, and **per-member utilization**, alongside hit rate and
  load spread.

The model is validated by sanity tests (`tests/test_bench_routing.py`): TTFT
percentiles are ordered; utilization ∈ (0,1]; throughput ≤ offered load; **uniform
load ⇒ symmetric queues**; **zero cache ⇒ `cache_aware` ≈ `least_busy`**; and β
monotonicity (higher β → lower tail latency and tighter load spread; lower β → higher
hit rate).

### 2. Re-validated default β = 0.5 (changed from 0.25)

With queueing modeled, **load imbalance now costs latency**, so the β selection rule
is restated in latency terms:

> Among the swept β, keep those that (a) hold the low-pressure `long_lived` load
> spread ≤ **1.25×** (max/min served) and (b) are **no worse than `sticky` on p95
> TTFT by more than 10%** in *any* scenario. Pick the **smallest eligible β** (the
> least spreading that already meets the bounds — i.e. the most cache reuse).

Applied to the scenario × β matrix (`--select-beta`), this selects **β = 0.5**:

| β | long_lived spread | balance ok | worst p95 vs sticky | eligible |
|---|---|---|---|---|
| 0.0 | 3.34× | no | +312% | no |
| 0.1 | 3.23× | no | +168% | no |
| 0.25 | 1.35× | no | +0% | no |
| **0.5** | **1.11×** | **yes** | **+0%** | **YES (smallest)** |
| 1.0 | 1.04× | yes | +0% | yes |
| 2.0 | 1.05× | yes | +0% | yes |

The headline finding: **the old default β = 0.25, re-measured under the queueing
model with Poisson arrivals, now breaks its own balance bound (1.35× > 1.25×)** and
leaves large tail latency on the table (long_lived p95 190 ms at β=0.25 vs 27 ms at
β=0.5). β = 0.25 was an artifact of a harness that could not price imbalance as
latency. **β = 0.5** is the smallest β that is balanced *and* no worse than sticky on
p95 anywhere; it also matches the llm-d/KServe ≈2:1 prefix:load weighting cited in
ADR 0010 (effective β ≈ 0.5). Larger β (≥1.0) over-spreads and gives up cache reuse
(cache_pressure hit rate collapses from 46% at β=0.5 to 37% at β=1.0, barely above
sticky's 33%). The config default (`BROKER_CACHE_AWARE_BETA`) is changed to `0.5`;
`sticky` remains the default *strategy* and all other defaults reproduce v0.5.0.

At β = 0.5, `cache_aware` beats `sticky` on p95 TTFT in **all four** scenarios and on
hit rate in the three reuse-heavy scenarios. The ADR 0010 "upper bound" caveat is
**removed** for these claims (the queueing model now supports them) and replaced by
the honest statement that the numbers are a *simulation* calibrated to the published
service model, not a measurement of a specific GPU.

The `skewed_bursty` "when to use" guidance is **updated honestly**: under the
queueing model, `cache_aware` β=0.5 now *beats* `sticky` there on p95 (1.17 s vs
8.3 s) and throughput (+18%) because spreading relieves the queue; `sticky` retains a
higher raw hit rate (44% vs 36%). So `cache_aware` is no longer a loss under bursty
skew — the remaining reason to keep `sticky` the default is conservatism (unchanged
behavior) and workloads where per-session pinning is contractually required.

### 3. Output-length / queue-aware load signal — Rejected (with data)

The bench compares three load signals through the real scorer: `count` (in-flight
requests, the ADR 0010 default), `chars` (in-flight prompt characters — a
dependency-free prefill-work proxy the broker *can* compute), and `work` (remaining
service iterations — an oracle needing an output-length predictor the broker does not
own). At the default β, neither alternative gives a consistent win: `chars` is within
noise of `count` (e.g. cache_pressure hit 47.4% vs 46.2%, but cross_session p99
*worse*: 667 vs 406 ms), and the `work` oracle trades a little more hit rate for
*worse* p95/p99 (decode work inflates the signal but does not affect TTFT). **Reject**
a queue/work-aware load signal: plain in-flight `count` is simplest and already
competitive, and operators who want a real queue-depth signal already have the
non-loading vLLM `/metrics` scrape (`BROKER_CACHE_AWARE_USE_METRICS`, ADR 0010). The
bench keeps `--load-signal` so the decision stays reproducible.

### 4. Cross-replica LRU size bound under Redis — Implemented

ADR 0010's prefix-index size bound was a **per-replica** in-process `OrderedDict`, so
under Redis N replicas could hold up to N× the intended entries (only the TTL bounded
the shared index). We add a generic **sorted-set LRU** to the `StateStore`
(`zlru_touch` / `zlru_trim` / `zlru_size`): each index key is scored by last-access
time; `zlru_trim` evicts the oldest beyond `max_entries` and returns them so the
caller deletes the associated KV entries. `CacheRouter.record` now trims through it,
so under Redis the sorted set is **shared and the size bound holds across replicas**
(the in-process backend keeps the identical per-process semantics it always had). The
TTL remains a second, time-based bound. Tested with `fakeredis`, including a
two-replica test asserting the combined index is capped at `max_entries`, not 2×.

### 5. Token-ID block hashing — Rejected

Needs a per-model tokenizer the broker deliberately does not own; a dependency-free
path would mean bundling per-backend tokenizers — exactly the coupling ADR 0010
avoids. Character blocks stay (SGLang precedent: raw text, no tokenization).

### 6. Precise KV-event (ZMQ) index and Preble-style prefix autoscaling — Rejected

The precise KV-event index couples the broker to vLLM/SGLang internals and requires
every pod wired for event streaming, violating the backend-agnostic invariant.
Preble-style prefix autoscaling (replicating a hot prefix across GPUs) is a
scheduler/placement feature the broker does not own. Both out of scope; the optional
`/metrics` scrape (ADR 0010) remains the one real-signal concession.

## Alternatives considered

- **Keep the no-queueing bench and β = 0.25.** Rejected: it makes a latency claim the
  model cannot support and, measured honestly, hot-spots the low-pressure pool.
- **Select β by minimizing fleet mean p95 TTFT.** Rejected: over-weights the
  near-overload scenarios and drives β toward `least_busy` (β≥1), discarding the cache
  benefit the feature exists for. "Smallest β meeting the balance + no-worse-than-
  sticky bounds" keeps the most reuse while fixing the tail.
- **Analytical M/M/c (Erlang-C) instead of DES.** Used only as a *sanity oracle* for
  tests; the DES is needed because real workloads have shared prefixes, bursty
  arrivals, and finite caches that closed forms do not capture.
- **Implement a chars/work load signal anyway.** Rejected on the bench data (§3).

## Consequences

- **Positive** — the bench now prices load imbalance as latency, so β is tuned
  against the metric operators care about (TTFT p95/p99), not a proxy. The new default
  β = 0.5 removes a real hot-spotting tail at low pressure, beats `sticky` on p95 in
  every scenario, and matches production-stack weightings. The Redis size bound is now
  truly cross-replica. All ADR 0010 deferrals are closed.
- **Negative / cost** — β = 0.5 trades a few points of raw hit rate for much better
  tail latency and balance (a deliberate, documented tradeoff). The bench is a
  *simulation*: absolute millisecond figures depend on the service-model constants
  (`iter_base_ms`, `iter_per_seq_ms`, `member_slots`, arrival rates), which are
  documented and overridable; the *rankings and the β decision* are what the bench is
  for, and they are robust across the scenario matrix.
- **Risk** — a deployment whose prompt-length / reuse / load distribution differs from
  the scenarios may prefer a different β; `--matrix` / `--select-beta` /
  `--capacity-sweep` let operators re-tune, and `/status` exposes live hit rate +
  per-member in-flight to validate in production. Changing the default β is a
  behavior change only for deployments that had *opted in* to `cache_aware` without
  setting β explicitly; `sticky` (the default strategy) is unaffected.

## Confirmation

- **Queueing model**: `scripts/bench_routing.py` discrete-event sim; validated by
  `tests/test_bench_routing.py` — percentile ordering, bounded utilization,
  throughput ≤ offered, **uniform ⇒ symmetric queues**, **zero-cache ⇒ cache_aware ≈
  least_busy**, and β monotonicity (tail latency, load spread, hit rate).
- **Re-validated β**: `tests/test_bench_routing.py` pins `Settings().cache_aware_beta
  == 0.5`, asserts cache_aware ≥ sticky on hit rate AND p95 for `cache_pressure` /
  `cross_session`, asserts the default β holds the 1.25× balance bound while **β=0.25
  now breaks it**, and unit-tests the selection rule (synthetic matrices + a real
  reduced-matrix integration test that rejects 0.25 and picks the config default).
  `tests/test_config.py` pins the new default.
- **Redis cross-replica LRU (item 4)**: `broker/state.py` `zlru_*` on both backends;
  `broker/cache_routing.py` trims through it; `tests/test_cache_routing.py` covers the
  in-process and `fakeredis` bounds and a two-replica shared-bound test.
- **Rejections (items 3, 5, 6)**: recorded above with rationale; item 3 is backed by
  the `--load-signal` bench comparison; `docs/roadmap.md` marks each ✅/❌ with a link.

All quality gates pass: ruff check + format, mypy (broker), full pytest on the shared
venv and the private `[dev,redis,otel]` extras venv, the Python 3.11 container gate,
and the v0.5.0 backward-compatibility suite.
