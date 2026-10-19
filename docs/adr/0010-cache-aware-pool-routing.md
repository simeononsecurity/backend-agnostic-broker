# ADR 0010: Cache-aware pool routing (prefix affinity + load-aware scoring)

- Status: Accepted
- Date: 2026-10-19
- Supersedes: none (extends ADR 0003 routing, builds on ADR 0009 StateStore)
- Related: `docs/roadmap.md` item 6; `docs/routing-and-health.md`;
  `docs/research/adr-0010-cache-aware-pool-routing.md` (cited prior art); prior
  art: vLLM production-stack `LoadAwareRouter`, llm-d inference scheduler, SGLang
  router, AIBrix gateway, Preble (arXiv 2407.00023)

## Context

A *public model name* can map to a pool of interchangeable members (ADR 0003).
Today's strategies are `sticky` (default — pin a session to a member), `least_busy`,
and `round_robin`. Sticky pins a *session* to a member, which is only a **coarse
proxy** for KV-cache locality: it captures a single conversation's growing prefix
but ignores prefixes *shared across sessions* — system prompts, few-shot
preambles, RAG contexts — and it can hot-spot a popular member.

Production inference stacks route on the prompt **prefix** instead: send a request
to the replica that already holds its prompt's KV blocks (reusing cache, skipping
prefill) while spreading load so a cache-warm replica doesn't saturate. The
research note surveys llm-d, vLLM production-stack, SGLang, AIBrix, and Preble;
they converge on two terms — a prefix-match benefit and a load penalty — and
differ mainly in how the index is built (precise KV-event stream vs approximate,
router-maintained) and how the tradeoff is expressed (a single weight vs a
threshold band). llm-d reports ~3× throughput / ~2× faster TTFT vs round-robin
from prefix-aware routing. This is the single biggest throughput/latency lever for
pools, and the broker already has the shared-state substrate (ADR 0009) to hold
the index.

The broker's constraints shape the design: it is **backend-agnostic** (must not
couple to vLLM/SGLang internals), owns **no tokenizer**, must keep **default
behavior unchanged**, and must preserve the health/cooldown/capability filtering,
failover, and no-silent-cross-model-fallback invariants.

## Decision

We add an **opt-in** `cache_aware` route strategy (`BROKER_ROUTE_STRATEGY=cache_aware`);
`sticky` stays the default and all defaults reproduce v0.4.0 exactly.

**Prefix index (approximate, router-maintained).** We normalize each request's
prompt (chat `messages` as `role:content` in order; completion `prompt`; responses
/ embeddings `input`), slice it into fixed-size **character** blocks
(`BROKER_CACHE_AWARE_BLOCK_CHARS`, default 128 ≈ 32 tokens), and compute a
**chained** hash per block (`h_i = sha256(h_{i-1} + block_i)`, truncated). Chaining
means a single block hash identifies the *entire* prefix up to it, so a
longest-prefix match is just "count the leading consecutive blocks a member
holds". Only full blocks are hashed (a trailing partial block is dropped, like
vLLM). The index maps `prefix-block -> member(s)` and lives on the ADR 0009
`StateStore` (in-process by default, shared via Redis when `BROKER_REDIS_URL` is
set), with a per-replica LRU size bound (`BROKER_CACHE_AWARE_MAX_ENTRIES`, default
10000) and a TTL (`BROKER_CACHE_AWARE_TTL_SECONDS`, default 1800) approximating
KV-cache residency. After each successful serve the chosen member's served blocks
are recorded.

**Scoring (vLLM production-stack `LoadAwareRouter`).** After the existing
health/cooldown/capability filtering produces the candidate tier, we score every
candidate:

```
score(m) = matched_fraction(m) - beta * relative_load(m)
relative_load(m) = (load(m) - mean_load) / max(1, mean_load)
```

and route to the argmax. `matched_fraction` is normalized to [0,1] so one `beta`
is the same policy at any prompt length; `relative_load` is normalized to the pool
mean so `beta` is deployment-independent. `beta` (`BROKER_CACHE_AWARE_BETA`,
**default 0.25**) is the single tunable: 0 = pure prefix affinity, higher = spread
harder. When **no** member holds any prefix (e.g. a new system prompt, or a prompt
shorter than one block) we fall back to `least_busy`. Tie-breaks are deterministic:
highest score, then least load, then round-robin.

**Default `beta` = 0.25 (retuned from the benchmark, not inherited).** The vLLM
production-stack `LoadAwareRouter` ships `beta=1.0`, but it routes on a *precise*,
token-exact KV index; the broker's *approximate*, character-block,
record-after-serve index is a weaker match signal against the same load term, so
`beta=1.0` over-penalizes affinity. The `scripts/bench_routing.py` scenario×β matrix
(long_lived / cache_pressure / cross_session / skewed_bursty) confirms `beta=1.0`
never beats `sticky`, while **`beta=0.25`** roughly doubles the hit rate under cache
pressure (31.8%→64.8%) and lifts cross-session hit rate (77.7%→91.3%, p95
1000→467 ms) at acceptable balance. Selection rule: *the smallest swept β whose load
spread stays within 25% on the low-pressure `long_lived` scenario* (β=0.1 breaks it
at 1.51×; β=0.25 holds at 1.17×), which also maximizes the cache-pressure gain. This
matches llm-d/KServe, which weight the prefix scorer ≈2× the load scorer (effective
`beta ≤ 0.5`). `cache_aware` is **not** a universal win: under bursty, skewed
high-concurrency (`skewed_bursty`) `sticky`'s durable pin beats record-after-serve
routing — documented as a "when to use" guide in `docs/routing-and-health.md`, and
`sticky` remains the default strategy.

**Benchmark caveat — TTFT/p95 are a simulated upper bound.** The harness models
latency as pure routing savings (`TTFT = base − hit_fraction × saved`) with **no
queueing delay from load imbalance**. At `beta=0.25` the load spread is real
(≈2.4× on `cross_session`, ≈1.75× on `cache_pressure`), so a real backend's hot
member would add queueing the model omits: the **hit-rate** results are routing-only
and robust, but the **TTFT/p95 wins are an upper bound**. Validate the latency claim
on a real vLLM pool with `BROKER_CACHE_AWARE_USE_METRICS=true` before relying on it;
adding a queueing model to the bench is a deferred follow-up (see `docs/roadmap.md`).

**Load signal.** `load` defaults to the in-flight request count. Optionally
(`BROKER_CACHE_AWARE_USE_METRICS=true`) the existing **non-loading** health probe
scrapes an exposed vLLM `/metrics` endpoint for `vllm:num_requests_waiting` as a
real queue-depth signal, falling back to in-flight counts when absent. The probe
never loads a model to read metrics.

**Interplay with sticky.** `cache_aware` is a *replacement* selection method, not
an addition to sticky: when it is active the sticky session map is not used, and
prefix affinity provides the locality that session pinning approximated (a
conversation's growing prefix naturally keeps hitting its warm member, and
cross-session shared prefixes are captured too). Failover, capability filtering,
cooldowns, and the no-cross-model rule are unchanged.

## Alternatives considered

- **Keep sticky-by-session only** — simple, but ignores cross-session shared
  prefixes (system prompts, few-shot, RAG) and can hot-spot. Retained as the
  default; `cache_aware` is the opt-in upgrade.
- **Precise KV-event (ZMQ) index** (llm-d / AIBrix KV-sync) — route on each
  engine's own `BlockStored`/`BlockRemoved` events for highest fidelity. Rejected
  for now: it couples the broker to vLLM/SGLang internals and needs every pod
  wired for event streaming, violating the backend-agnostic invariant. **Deferred**
  (see Consequences / roadmap).
- **Token-ID block hashing** (vLLM APC, AIBrix) — more precise block boundaries,
  but needs a per-model tokenizer the broker deliberately does not own. We hash
  characters instead (SGLang precedent: raw text, no tokenization overhead).
- **Explicit `cache_threshold` band / SGLang dual-mode switch / AIBrix stddev
  gate** — alternatives to the additive `beta` argmax, not complements. We adopt
  the single-`beta` additive score (one knob; the argmax handles small matches
  gracefully, per the production-stack rationale).
- **Prefix autoscaling / output-length-aware load** (Preble, PREBLE+S3) —
  scheduler features out of a router's scope; deferred.

## Consequences

- **Positive** — big TTFT/throughput win on pools serving shared prefixes
  (multi-turn chat, shared system prompts, few-shot, RAG); cross-session reuse
  that sticky cannot capture; one intuitive knob (`beta`); the index is
  Redis-shareable across replicas for free via ADR 0009; `/status` exposes index
  size + hit rate for tuning.
- **Negative** — an approximate index can be stale (mitigated by TTL) and the
  per-replica LRU size bound is local under Redis (TTL is the cross-replica
  bound); character blocks are coarser than token blocks; negligible benefit for
  singleton (non-pooled) models or very short prompts.
- **Risks** — a too-small `beta` hot-spots a popular prefix onto one member
  (mitigation: the default `beta=0.25` was tuned from the scenario×β matrix to stay
  balanced under low pressure; `/status` hit rate + per-member in-flight make it
  observable; `docs/troubleshooting.md` covers it). A too-large `block_chars` makes matching coarse and misses partial reuse (tunable; the
  benchmark helps pick it). Hash collisions are astronomically unlikely with
  chained sha256 and only affect routing quality, never correctness.

## Confirmation

Implemented in `broker/cache_routing.py` (normalization, chained hashing,
`CacheRouter` index + scoring inputs) and `broker/manager.py` (`_select_cache_aware`,
record-on-success, `/status` stats, optional metrics scrape in the non-loading
probe). Verified by `tests/test_cache_routing.py` (25 tests), covering:

- `normalize_prompt` across messages/completion/input/multimodal parts, and
  **chained** block hashing (early-block change propagates; shared prefix shares
  leading hashes; only full blocks; `MAX_PREFIX_BLOCKS` bound);
- longest-prefix `match_fractions`, no-match empty result, LRU eviction, TTL
  expiry, and hit-rate stats;
- manager routing: repeat-prefix → warm member; no-match → `least_busy`;
  load penalty flips the choice; `beta=0` is pure affinity; three-member
  longest-prefix wins;
- invariants preserved: unhealthy and cooled-down members excluded, failover and
  all-down-raises still hold, single-member pools, sticky map untouched under
  cache_aware, default strategy builds no router, embeddings routed too;
- the index on the `StateStore` for both backends (in-process always; Redis via
  `fakeredis` when the extra is installed, else skipped);
- `_parse_waiting` vLLM `/metrics` parsing; a `scripts/bench_routing.py` `--smoke`
  subprocess run; and `tests/test_bench_routing.py`, a fast deterministic
  regression test asserting the **default `beta`** (`Settings().cache_aware_beta`)
  beats `sticky` on hit rate *and* TTFT p95 for the `cache_pressure` and
  `cross_session` scenarios, and that the default β keeps the `long_lived` load
  spread within the documented 25% balance bound.

The scenario×β matrix from `scripts/bench_routing.py --matrix` (the default
`beta=0.25` roughly doubles cache-pressure hit rate and wins cross-session, while
`beta=1.0` never beats sticky and `skewed_bursty` is an honest sticky win) is
recorded in `docs/routing-and-health.md` with the default-β selection rule and a
"when to use cache_aware vs sticky" guide.
