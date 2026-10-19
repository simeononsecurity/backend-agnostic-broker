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
  queueing**, **prefill work (uncached prefill tokens computed)**, **throughput**, and
  **per-member utilization**, alongside hit rate and load spread. A `--sweep` mode runs
  every strategy across load levels (~50/70/85/95% max utilization) and bisects each
  strategy's **capacity** (max arrival rate under a TTFT-p95 SLO).

The model is validated by sanity tests (`tests/test_bench_routing.py`): TTFT
percentiles are ordered; utilization ∈ (0,1]; throughput ≤ offered load; **uniform
load ⇒ symmetric queues**; **zero cache ⇒ `cache_aware` ≈ `least_busy`**; and β
monotonicity (higher β → lower tail latency and tighter load spread; lower β → higher
hit rate).

### 2. Re-validated default β = 0.5 (changed from 0.25), against the right baseline

With queueing modeled, **load imbalance now costs latency**. The β selection rule is
therefore restated in latency terms, and — correcting the first pass of this ADR, which
compared `cache_aware` only against `sticky` at a single **saturated** (~99% util)
operating point — it is evaluated **against `least_busy` (the tail-latency-best
strategy) at a moderate, non-saturated load**:

> Evaluated at ~85% max utilization: among the swept β, keep those that (a) hold the
> low-pressure `long_lived` **served-count** spread ≤ **1.25×** (max/min) and (b) are
> **no worse than `least_busy` on p95 TTFT by more than 10%** in *any* scenario. Pick
> the **smallest eligible β** — the least spreading, hence the most cache reuse / the
> least prefill work (the metric `cache_aware` exists to improve).

Applied to the scenario × β matrix (`--select-beta`), this selects **β = 0.5**:

| β | long_lived spread | balance ok | worst p95 vs least_busy | p95 ok | eligible |
|---|---|---|---|---|---|
| 0.0 | 4.69× | no | +48% | no | no |
| 0.1 | 3.01× | no | +39% | no | no |
| 0.25 | 1.50× | no | +13% | no | no |
| **0.5** | **1.10×** | **yes** | **+8%** | **yes** | **YES (smallest)** |
| 1.0 | 1.07× | yes | +0% | yes | yes |
| 2.0 | 1.07× | yes | +6% | yes | yes |

The old default **β = 0.25 fails on both counts** at a realistic load: it hot-spots the
low-pressure pool (served spread 1.50× > 1.25×) *and* is 13% worse than `least_busy` on
p95 in `cross_session`. β = 0.25 was an artifact of a balance-only heuristic on a
harness that could not price imbalance as latency. **β = 0.5** is the smallest β that is
balanced *and* within 10% of `least_busy` on p95 everywhere; it also matches the
llm-d/KServe ≈2:1 prefix:load weighting cited in ADR 0010 (effective β ≈ 0.5). Larger β
(≥1.0) over-spreads and gives up cache reuse (cache_pressure hit rate falls from 46% at
β=0.5 to 32% at β=1.0, barely above sticky). The config default
(`BROKER_CACHE_AWARE_BETA`) is changed to `0.5`; `sticky` remains the default *strategy*
and all other defaults reproduce v0.5.0.

### 2a. Honest conclusions — baseline, load range, and what `cache_aware` is for

The first pass of this ADR overstated `cache_aware` by framing it only against `sticky`
at one saturated point. Corrected (`scripts/bench_routing.py --sweep`, all *simulated*):

- **`least_busy` is the tail-latency-best strategy.** It has the lowest p95 TTFT **and**
  the highest p95-SLO capacity in **every** scenario and at every load level. The docs
  must not imply `cache_aware` is latency-best — it is not.
- **Results at one load don't generalize; sweep the load.** Below ~90% utilization, p95
  TTFT is tiny and nearly identical across strategies; the strategies only separate as
  the pool approaches saturation. `--sweep` runs all four strategies at ~50/70/85/95%
  max utilization (arrival rate calibrated per scenario via `least_busy`).
- **Throughput at fixed open-loop arrivals is meaningless** (every strategy completes ≈
  the offered rate), so it is no longer a headline. The metric that *is* meaningful and
  that `cache_aware` improves is **uncached prefill tokens computed (GPU work)**: at
  ~85% util `cache_aware` β=0.5 computes 20–48% fewer than `least_busy` (cross_session
  52→27, cache_pressure 183→139, long_lived 37→29 tokens/req) and lands a much higher
  hit rate (cache_pressure 26%→46%, cross_session 75%→91%).
- **Capacity under an SLO (p95 ≤ 200 ms, found by bisection):** `least_busy` ≥
  `cache_aware` > `round_robin` > `sticky` everywhere. `cache_aware` trails `least_busy`
  by only ~2–5% and beats `sticky` by 15–55%. The SLO (200 ms) is a common interactive
  first-token budget, chosen to sit ~an order of magnitude above the unloaded
  batched-prefill latency and above the bursty service floor so capacity measures the
  onset of queueing, not the service floor.
- **When to use which** (see `docs/routing-and-health.md#when-to-use-which-strategy`):
  `least_busy` for tail latency / p95-capacity / simplicity; `cache_aware` (β=0.5) for
  GPU work saved / prefill-compute-bound capacity / shared-prefix workloads;
  `sticky` (the conservative default) for contractual session pinning or highest raw hit
  rate under skew.
- **The "roughly doubles hit rate" claim is weakened.** cache_pressure hit rate goes
  32.6% → 46.2% (**1.4×**, not 2×); cross_session 75% → 91% (1.2×). The honest statement
  is "materially higher hit rate on shared-prefix workloads", not "doubles".

The `skewed_bursty` guidance is likewise corrected: `cache_aware` β=0.5 does **not** beat
`least_busy` on p95 there (it is ~within noise, and `least_busy` leads on p95-SLO
capacity 12.7 vs 12.1); it trades `sticky`'s higher raw hit rate (44% vs ~31%) for a far
better tail than `sticky`. So the reason to keep `sticky` the default remains
conservatism (unchanged behavior) and contractual per-session pinning — not a claim that
`cache_aware` wins the tail.

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

- **Positive** — the bench now prices load imbalance as latency and reports it across a
  **load sweep** (not one saturated point) against the right baseline (`least_busy`), so
  β is tuned against the metric operators care about (TTFT p95) with honest conclusions.
  The new default β = 0.5 removes a real hot-spotting tail at low pressure, stays within
  ~10% of `least_busy` on p95, and matches production-stack weightings. The bench also
  reports **prefill work (uncached tokens computed)** and **SLO capacity**, which is
  where `cache_aware` actually wins (20–48% less work, higher hit rate). The Redis size
  bound is now truly cross-replica. All ADR 0010 deferrals are closed.
- **Negative / cost** — `cache_aware` is **not** the tail-latency-best strategy;
  `least_busy` is (lowest p95 and highest p95-SLO capacity in every scenario). Choosing
  `cache_aware` means optimizing for GPU work / hit rate, accepting p95 within ~10% of
  `least_busy`. β = 0.5 trades a few points of raw hit rate for better balance (a
  deliberate, documented tradeoff). The bench is a *simulation*: absolute millisecond
  figures depend on the service-model constants (`iter_base_ms`, `iter_per_seq_ms`,
  `member_slots`, arrival rates), which are documented and overridable; the *rankings,
  the work-saved, and the β decision* are what the bench is for, and they are robust
  across the scenario matrix and load levels.
- **Risk** — a deployment whose prompt-length / reuse / load distribution differs from
  the scenarios may prefer a different β or a different strategy; `--sweep` / `--matrix`
  / `--select-beta` / `--capacity-sweep` let operators re-tune, and `/status` exposes
  live hit rate + per-member in-flight to validate in production. Changing the default β
  is a behavior change only for deployments that had *opted in* to `cache_aware` without
  setting β explicitly; `sticky` (the default strategy) is unaffected. The v0.5.0
  backward-compat suite shows exactly **3 intended failures**, all from this β default
  change (0.25 → 0.5): `test_default_beta_is_the_retuned_value`,
  `test_cache_pressure_default_beta_substantially_lifts_hit_rate` (the v0.5.0 pin needed
  ≥1.5× sticky hit rate; β=0.5 gives 46.2/32.6 = 1.42×), and
  `test_config::test_cache_aware_defaults_preserve_legacy`.

## Confirmation

- **Queueing model**: `scripts/bench_routing.py` discrete-event sim; validated by
  `tests/test_bench_routing.py` — percentile ordering, bounded utilization,
  throughput ≤ offered, **uniform ⇒ symmetric queues**, **zero-cache ⇒ cache_aware ≈
  least_busy**, β monotonicity (tail latency, load spread, hit rate), and the
  prefill-work metric's internal consistency.
- **Re-validated β (vs least_busy, moderate load)**: `tests/test_bench_routing.py` pins
  `Settings().cache_aware_beta == 0.5`; asserts `cache_aware` **saves prefill work** and
  **lifts hit rate vs `least_busy`** for `cache_pressure` / `cross_session`, and that its
  p95 is within the rule's tolerance of `least_busy` at ≤85% util; asserts `least_busy`
  is **tail-best at saturation** (p95 ≤ cache_aware < sticky); asserts the default β holds
  the 1.25× balance bound while **β=0.25 fails a selection bound** at moderate load;
  checks **SLO capacity** (`cache_aware` > `sticky`, `least_busy` leads); exercises the
  **utilization sweep + capacity** helpers; and unit-tests the (least_busy-referenced)
  selection rule on synthetic matrices + a real reduced-matrix integration test that
  rejects 0.25 and picks the config default. `tests/test_config.py` pins the new default.
- **Redis cross-replica LRU (item 4)**: `broker/state.py` `zlru_*` on both backends;
  `broker/cache_routing.py` trims through it; `tests/test_cache_routing.py` covers the
  in-process and `fakeredis` bounds and a two-replica shared-bound test.
- **Rejections (items 3, 5, 6)**: recorded above with rationale; item 3 is backed by
  the `--load-signal` bench comparison; `docs/roadmap.md` marks each ✅/❌ with a link.

All quality gates pass: ruff check + format, mypy (broker), full pytest on the shared
venv and the private `[dev,redis,otel]` extras venv, the Python 3.11 container gate,
and the v0.5.0 backward-compatibility suite (3 intended β-default failures above).
