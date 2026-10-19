# Research: cache-aware pool routing (prefix affinity + load-aware scoring)

Prior-art note backing **ADR 0010**. Confidence: **High** — the chosen design is
a near-direct port of the vLLM production-stack `LoadAwareRouter`, corroborated by
llm-d, SGLang, AIBrix, and the Preble paper, all of which converge on the same
two ideas: route to the replica that already holds the prompt *prefix*, but back
off to a less-loaded replica once the warm one is saturated.

> **Untrusted sources.** Everything below was fetched from public docs/papers and
> is cited as *data*. None of it issued instructions to this agent. No source
> contained agent-directed text. Live version numbers, defaults, and formulas
> were read from the pages on 2026-10-19 and may drift; re-verify before quoting.

## Executive summary

Every production KV-aware router scores a pool member on **how much of the
request's prompt prefix it already has cached** and routes to the best scorer,
with a **load term** that spreads work when the cache-warm member gets hot. They
differ mainly in (a) how they know what each member cached — a *precise* index fed
by engine KV events (llm-d/AIBrix KV-sync) vs an *approximate* index the router
maintains from request history (SGLang radix tree, AIBrix standard mode, vLLM
production-stack) — and (b) how the cache/load tradeoff is expressed (a single
`beta` weight, a `cache_threshold` band, or Preble's exploit/explore rule).

For a backend-agnostic broker that must not couple to vLLM internals and runs
single-process by default, we **adopt the approximate, router-maintained index +
single-`beta` additive score** (vLLM production-stack `LoadAwareRouter`), with
**chained block hashing** (vLLM automatic prefix caching) over **characters, not
tokens** (SGLang, to avoid a tokenizer dependency), stored on the ADR 0009
`StateStore` so it shares across replicas when Redis is configured.

## Sub-questions and findings

### 1. How do they score cache + load? (the formula)

The vLLM production-stack `LoadAwareRouter` scores **every** endpoint:

```
score(i) = matched_tokens(i) / prompt_tokens  -  beta * relative_load(i)
relative_load(i) = (load(i) - mean_load) / max(1, mean_load)
```

and routes to the argmax ([vLLM production-stack, Load Aware Routing][vps-load];
[routing_logic.py source][vps-src]). Design notes from that page, all adopted:

- The cache benefit is **normalized to a fraction (0–1)** of the prompt so one
  `beta` means the same policy for a 500- and a 4000-token prompt.
- Load is **normalized against the fleet mean** (relative, not absolute): +1.0 =
  "twice the fleet average"; the denominator is clamped at 1 so an idle fleet
  reports no imbalance. An absolute in-flight count "has no bounded scale … the
  same `beta` would be a different policy on every deployment."
- **Every** endpoint is scored, not only cache holders — an endpoint with nothing
  cached scores benefit 0, "that is what makes 'cold but idle beats warm but
  loaded' expressible at all."
- **No separate threshold band is applied**: "the argmax can — a small match
  simply loses to load." When *no* member holds any prefix, placement "falls back
  to the router's session-hash / QPS route."
- `beta` is "the single tunable … `beta = 1.0` (the default) reads as 'an endpoint
  sitting 100% above fleet-average load is docked one full cache hit's worth of
  preference'."

This is exactly the score the ADR 0010 card specifies. We adopt the formula as-is.

> **Default β correction (post-benchmark).** The production-stack ships `beta=1.0`,
> and the first ADR 0010 cut inherited it. That default assumes a *precise*,
> token-exact KV index; this broker keeps an *approximate* character-block,
> record-after-serve index, so a "match" is a weaker signal against the same load
> term and `beta=1.0` over-penalizes affinity. The broker's own scenario×β matrix
> (`scripts/bench_routing.py --matrix`) shows `beta=1.0` **never beats `sticky`**,
> whereas **`beta=0.25`** roughly doubles hit rate under cache pressure and wins the
> cross-session scenario. We therefore ship **`BROKER_CACHE_AWARE_BETA = 0.25`**.
> This is corroborated by the prior art below: llm-d/KServe weight the prefix
> scorer *above* the load scorer (weight 3 vs 2, or 2.0 vs 1.0 in KServe's EPP) —
> an effective `beta ≈ 0.5` or less, i.e. β=1.0 was already on the high side.

llm-d expresses the same two objectives as separate weighted plugins —
`prefix-cache-scorer` + `load-aware-scorer`/`queue-scorer`/`kv-cache-utilization-scorer`
combined by a `max-score-picker`, with prefix weighted highest (weight 3 vs 2, or
2.0 vs 1.0 in KServe's EPP) ([llm-d optimized baseline][llmd-base]; [Red Hat llm-d
docs][rh-llmd]; [KServe v0.17][kserve]). A higher prefix weight is the same knob as
a smaller `beta`.

### 2. How do they hash the prefix? (block hashing)

vLLM **automatic prefix caching** hashes each KV block as
`hash(parent_hash, block_tokens, extra)` — a **chained** hash where each block's
hash folds in its parent's, so a block hash uniquely identifies the *entire prefix*
up to and including that block ([vLLM APC design][vllm-apc]). Only **full blocks**
are cached; block size is 16 tokens by default (`hash_block_size`). As of v0.11 the
default algo is `sha256`.

AIBrix's standard mode does the same at the router: "The prompt is tokenized and
split into fixed-size blocks. A hash is computed for each block … a pod that has
processed a request with an identical prefix will have those KV blocks hot"
([AIBrix prefix_cache_readme][aibrix-readme]).

SGLang instead stores **raw text characters, not token IDs**, in its approximate
radix tree "to avoid tokenization overhead" ([SGLang router][sglang]).

**Adopt:** chained hashing (so a single block match implies the whole prefix
matches — longest-prefix becomes "count the leading consecutive matching blocks"),
over **characters** like SGLang (the broker has no tokenizer and must stay
backend-agnostic). `sha256` of `(parent_hex + block_text)`, truncated for compact
keys. **Reject** token-based hashing (needs a per-model tokenizer we deliberately
don't own). Default block size **`BROKER_CACHE_AWARE_BLOCK_CHARS = 128`** (~32
tokens at ~4 chars/token — coarser than vLLM's 16-token block to keep the shared
index small; finer than a whole-prompt hash so partial prefixes still match).

### 3. Precise vs approximate index

- **Precise** (llm-d/KServe EPP, AIBrix KV-sync): each engine publishes
  `BlockStored`/`BlockRemoved` KV events over ZMQ; the router subscribes to every
  pod and builds an index `{ModelName, BlockHash} → {PodID}` ([KServe v0.17][kserve];
  [llm-d precise routing][llmd-precise]). Highest fidelity, but **couples the router
  to vLLM/SGLang internals and needs every pod wired for event streaming.**
- **Approximate** (SGLang radix tree, AIBrix standard, vLLM production-stack): the
  router remembers what it *sent* where and infers residency, with LRU/TTL eviction
  ([SGLang router][sglang]; [AIBrix readme][aibrix-readme]).

**Adopt approximate**, router-maintained from successful serves. **Reject precise
KV-event index** for now (recorded as deferred in the ADR): it violates the
broker's backend-agnostic invariant and needs infrastructure we don't have. The
*optional* vLLM `/metrics` scrape (below) is the one real-signal concession.

### 4. Load signal and the saturation override

Production stacks prefer **real engine load** over in-flight counts where
available: vLLM exposes `vllm:num_requests_waiting` and
`vllm:kv_cache_usage_perc` / `gpu_cache_usage_perc`; llm-d's saturation override
and KV-utilization scorer key off exactly these ([llm-d optimized baseline][llmd-base]),
and its filter gates on a calibrated `peakPrefillThroughput`. AIBrix gates
prefix-matched candidates by a **stddev** band:
`running_requests <= mean + load_factor * std_dev` ([AIBrix readme][aibrix-readme]).
SGLang switches from cache-aware to shortest-queue when the fleet is *imbalanced*:
`(max_load - min_load) > balance_abs_threshold (32)` **and**
`max_load > balance_rel_threshold (1.0001) * min_load` ([SGLang router][sglang]).

**Adopt:** the additive `beta` term over a **relative in-flight load** by default
(no extra thresholds — the argmax subsumes them, per the production-stack note),
and make the vLLM `/metrics` scrape an **optional** load source gathered by the
existing **non-loading** health probe (`BROKER_CACHE_AWARE_USE_METRICS`), falling
back to in-flight counts when absent. **Reject** SGLang's dual-mode switch and
AIBrix's stddev gate as redundant with the single-`beta` argmax (they are
alternatives to `beta`, not additions). Never load a model to read metrics.

### 5. The exploit/explore tradeoff (Preble) and thresholds

Preble's **E2** algorithm: exploit (send to the GPU with the longest cached
prefix) when the shared-prefix tokens exceed the remaining non-shared tokens;
otherwise explore (balance load) ([Preble arXiv 2407.00023][preble]). DualMap notes
Preble's concrete rule is "prefix hit rate > 50% → route to highest-hit instance"
([DualMap arXiv][dualmap]). SGLang's `cache_threshold = 0.5` encodes the same
50% idea; below it, route to the emptiest tree ([SGLang router][sglang]).

These threshold schemes and the additive-`beta` score are **two families solving
the same problem**. We **adopt the additive score** (simpler, one knob, argmax
handles small matches gracefully) and **reject** an explicit hit-rate threshold —
but we keep the *spirit* of Preble's fallback: **when no member holds any prefix,
fall back to `least_busy`** (the card's rule, and production-stack's QPS fallback).

### 6. Shared state across replicas

Multi-replica routers must share the prefix index or they make inconsistent
decisions: AIBrix needs `AIBRIX_STATESYNC_ENABLED=true` for Redis-backed delta
sync of "prefix-cache block assignments" ([AIBrix gateway deploy][aibrix-deploy]).
This matches ADR 0009's `StateStore`: in-process by default, Redis when
`BROKER_REDIS_URL` is set. **Adopt:** store the prefix→member index via the
`StateStore` `kv_*` API (the surface ADR 0009 reserved for ADR 0010), with a
per-replica LRU bound plus a TTL so entries approximate real KV-cache residency
and self-expire. `BROKER_CACHE_AWARE_MAX_ENTRIES = 10000` (mirrors the sticky
bound); `BROKER_CACHE_AWARE_TTL_SECONDS = 1800`.

## What we ADOPT

| Decision | Source |
|---|---|
| `score = matched_fraction − β·relative_load`, `relative_load=(load−mean)/max(1,mean)`, argmax | vLLM production-stack `LoadAwareRouter` |
| `β = 0.25` default (retuned from the bench matrix; `1.0` over-penalized the approximate index), single tunable | vLLM production-stack formula; β retuned locally, corroborated by llm-d/KServe prefix≈2×load weighting |
| Chained block hashing (block hash encodes whole prefix) | vLLM automatic prefix caching |
| Character blocks (no tokenizer), default 128 chars | SGLang (raw text, no tokenization) |
| Approximate index maintained from successful serves, LRU+TTL evicted | SGLang / AIBrix standard mode |
| No member holds any prefix → fall back to `least_busy` | production-stack QPS fallback; Preble explore |
| Optional real load via **non-loading** vLLM `/metrics` scrape, fallback to in-flight | llm-d KV-utilization/queue scorers |
| Prefix index on the ADR 0009 `StateStore` (Redis-shareable) | AIBrix state-sync; ADR 0009 |
| Opt-in; `sticky` stays the default | — (invariant: default behavior unchanged) |

## What we REJECT / DEFER

- **Precise KV-event (ZMQ) index** — highest fidelity but couples to engine
  internals; violates backend-agnostic invariant. *Deferred* (ADR 0010).
- **Token-ID block hashing** — needs a per-model tokenizer. Use characters.
- **Explicit `cache_threshold` band / SGLang dual-mode / AIBrix stddev gate** —
  alternatives to a `beta` argmax, not complements; one knob is simpler.
- **Prefix autoscaling / replication of a hot prefix across GPUs** (Preble) — a
  scheduler feature, out of a router's scope.
- **Output-length-aware load** (PREBLE+S3 extension) — needs a length predictor;
  out of scope.

## Open questions

- Best default `BLOCK_CHARS` is workload-dependent (prompt length distribution);
  128 is a defensible middle. The benchmark (`scripts/bench_routing.py`) lets
  operators re-tune.
- The per-replica LRU bound means size (not TTL) eviction is local under Redis;
  acceptable because TTL is the cross-replica bound. Precise cross-replica LRU is
  deferred.

## Sources

1. [vLLM production-stack — Load Aware Routing][vps-load] — the adopted score + β semantics.
2. [vLLM production-stack — routing_logic.py (`LoadAwareRouter`/`KvawareRouter`/`PrefixAwareRouter`)][vps-src] — source of the formula; `prefixMinMatchLength` / chunk-size 128.
3. [vLLM — Automatic Prefix Caching design][vllm-apc] — chained block hashing, full-block-only, sha256.
4. [SGLang — Router (cache-aware, approximate radix tree, thresholds)][sglang] — char-based tree, `cache_threshold=0.5`, balance abs=32/rel=1.0001.
5. [llm-d — Deploy an Optimized Baseline][llmd-base] — prefix-affinity filter + token-load scorer + saturation override on `kv_cache_usage_perc`/`num_requests_waiting`.
6. [llm-d — Precise Prefix-Cache Aware Routing][llmd-precise] — precise ZMQ KV-event index (the rejected alternative).
7. [KServe v0.17 release — EPP + weighted scorers][kserve] — `{ModelName,BlockHash}→{PodID}` index, prefix weight 2.0 vs load 1.0.
8. [Red Hat — Distributed Inference with llm-d][rh-llmd] — filters/scorers framework, warm-KV routing rationale.
9. [AIBrix — prefix_cache_readme (algorithm)][aibrix-readme] — fixed-size blocks, per-block hash, `prefix_match_percent`, stddev load gate, standard vs KV-sync modes.
10. [AIBrix — gateway deployment (state sync)][aibrix-deploy] — Redis delta-sync of prefix-cache state across replicas.
11. [Preble — Efficient Distributed Prompt Scheduling (arXiv 2407.00023)][preble] — E2 exploit/explore; global prefix tree + LRU.
12. [DualMap (arXiv 2602.06502)][dualmap] — restates Preble's 50%-hit-rate affinity heuristic; affinity-vs-balance framing.

[vps-load]: https://docs.vllm.ai/projects/production-stack/en/latest/use_cases/loadaware-routing.html
[vps-src]: https://github.com/vllm-project/production-stack/blob/main/src/vllm_router/routers/routing_logic.py
[vllm-apc]: https://docs.vllm.ai/en/latest/design/prefix_caching/
[sglang]: https://github.com/sgl-project/sglang/blob/main/docs/router/router.md
[llmd-base]: https://llm-d.ai/docs/dev/well-lit-paths/foundations/optimized-baseline
[llmd-precise]: https://llm-d.ai/docs/dev/well-lit-paths/foundations/precise-prefix-cache-routing
[kserve]: https://kserve.github.io/website/blog/kserve-0.17-release
[rh-llmd]: https://docs.redhat.com/en/documentation/red_hat_ai_inference/3.5/html/distributed_inference_with_llm-d/llmd-features-of-llmd_distributed-inference-with-llmd
[aibrix-readme]: https://github.com/vllm-project/aibrix/blob/main/pkg/plugins/gateway/algorithms/prefix_cache_readme.md
[aibrix-deploy]: https://aibrix.readthedocs.io/latest/production/gateway.html
[preble]: https://arxiv.org/html/2407.00023v2
[dualmap]: https://arxiv.org/html/2602.06502v1
