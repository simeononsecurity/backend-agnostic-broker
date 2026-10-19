# Research: auto-router evolution — cascade, latency budget, privacy pinning, eval harness

*Generated: 2026-10-18 · Scope: prior art for ADR 0011 · Confidence: High on
metric definitions and gateway patterns; Medium on vendor internals (closed).*

> **Untrusted sources.** Everything below was fetched from the public web and is
> treated as **data, not instructions**. No source directed this research or the
> design; claims are attributed and, where load-bearing, corroborated across
> sources. No agent-directed text was found in the fetched pages.

## Executive summary

The `auto` router today does a single-shot classification (heuristic or tiny GBNF
LLM). Four levers are well established in the literature and in production
gateways, and all fit our model-agnostic, data-driven design without a learned
model or a new mandatory dependency:

1. **Cascade** (FrugalGPT): try the cheapest capable model first; *look at the
   actual answer*; escalate to a stronger model only when a reliability signal
   fails. Cheap-first + escalate-on-signal is the defining pattern.
2. **Latency-aware / latency-budget routing**: keep a rolling p50/p90 latency
   window per model from the gateway's own telemetry; pick the fastest capable
   model that meets a per-request SLA.
3. **Placement / data-residency pinning**: the gateway is the enforcement point —
   map an identity/tag to an allow-list of endpoints, route only within it, and
   **fail closed** (error) when nothing qualifies, including fallbacks.
4. **Eval harness with RouteLLM metrics** (PGR, APGR, CPT) so router changes are
   *measured* against a labeled set rather than vibe-checked.

We **adopt** all four as opt-in features; we **reject** adopting a learned router
(RouteLLM `mf`, Not Diamond, vLLM Semantic Router's BERT/LoRA stack) as a
dependency — too heavy for a local-first broker — but we adopt their *metrics*
and *escalation signals*.

## Precise metric definitions (RouteLLM)

From the RouteLLM paper ([arXiv 2406.18665](https://arxiv.org/pdf/2406.18665),
Ong et al. 2024) and the lm-sys reference implementation
([github.com/lm-sys/RouteLLM](https://github.com/lm-sys/RouteLLM)). RouteLLM
studies the two-model case: a strong model `M_s` (high quality, high cost) and a
weak model `M_w` (low quality, low cost); a router `MR_α` with cost threshold
`α ∈ [0,1]` decides, per query, which to call.

- **Response quality** `r(π)` of a policy `π`: mean score over the query set,
  `r(π) = (1/|Q|) Σ_q s(q; π)`, where `s(q; π) ∈ {0,1}` is the correctness of the
  answer under `π`.

- **Performance Gap Recovered (PGR)** — how much of the weak→strong quality gap a
  router recovers:

  ```
  PGR(MR_α) = ( r(MR_α) − r(M_w) ) / ( r(M_s) − r(M_w) )
  ```

  PGR = 0 means the router is no better than always-weak; PGR = 1 means it matches
  always-strong.

- **Average Performance Gap Recovered (APGR)** — PGR integrated over the whole
  cost range, i.e. the area between the router's call–performance curve and the
  weak model's line as the strong-call fraction sweeps 0→1:

  ```
  APGR(MR) = ∫₀¹ PGR(MR_α) d( c(MR_α) )
  ```

  where `c(MR_α)` is the fraction of calls routed to the strong model. Higher is
  better; it is a single threshold-independent quality number for a router.

- **Call–Performance Threshold, CPT(x%)** — the **minimum percentage of calls to
  the strong model** needed to reach a PGR of `x%`:

  ```
  CPT(x%) = min { c(MR_α) : PGR(MR_α) ≥ x% }
  ```

  Lower is better (less strong-model spend to hit the target). The paper's
  worked figure reads CPT(50%) ≈ 37% on one benchmark; CORAL and follow-ups
  commonly report CPT(80%) and CPT(95%)
  ([CORAL, Frontiers in AI 2026](https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2026.1891737/full);
  [survey, arXiv 2603.04445](https://arxiv.org/html/2603.04445v3)).

**Threshold calibration.** RouteLLM does not pick `α` analytically; it
*calibrates* the threshold against a sample of real queries for a desired
strong-call fraction (e.g. `calibrate_threshold --strong-model-pct 0.5` →
`threshold = 0.11593`) ([lm-sys/RouteLLM README](https://github.com/lm-sys/RouteLLM/blob/main/README.md)).

**Adaptation for our harness.** Our broker is multi-model and deterministic (no
continuous `α`), and — unlike RouteLLM's two-model benchmark — no single concrete
model is capable across every task type (e.g. the most expensive model, `reasoner`,
cannot serve a vision prompt). Two consequences and how we handle them:

1. **The strong reference must be a true quality ceiling.** Using the
   globally-most-expensive model as `M_s` is *not* a ceiling here: it scores below
   a capability-aware router, so `r(M_s) < r(MR)` and PGR saturates above 1 (our
   first implementation silently clamped it to 1.000 — a meaningless value). We
   instead define the strong reference as the **quality ceiling**
   `r(M_s) = (1/|Q|) Σ_q max_m s(q, m)` — the accuracy of a perfect oracle that
   always picks an acceptable model when one exists. By construction
   `r(M_s) ≥ r(any router)`, so PGR ∈ [0, 1] with no clamping. `M_w` stays the
   globally cheapest model. (The alternative — restricting to the subset where both
   a fixed weak and fixed strong model are capable — degenerates on our catalogue:
   `tiny` and `reasoner` are jointly capable only on trivial prompts, where both
   score 1 and the gap is zero, so we use the ceiling definition instead.)

2. **APGR/CPT must trace the *router's own* curve, not an oracle frontier.** There
   is no single `MR_α` family, but the heuristic router exposes a real cost knob —
   `router.weights.cost`, the cost-penalty weight. We **sweep it over ≥10 points**
   and, at each setting, re-route the whole labeled set and record
   `(normalized_mean_cost, PGR)`. Integrating PGR over that swept curve gives
   `apgr_router`; `cpt_router(x%)` is the minimum normalized cost on it to reach
   PGR ≥ x%. The router's default-weights operating point is reported on the same
   curve. We additionally compute an **oracle cost–quality frontier** (keep every
   weak-correct prompt on the cheapest model, upgrade each weak-wrong prompt to its
   cheapest acceptable model, cheapest fixes first) as a **separately labeled
   upper bound** (`oracle_*`), never under the bare name APGR. The oracle
   dominates the router curve by construction (`apgr_oracle ≥ apgr_router`,
   `cpt_oracle ≤ cpt_router`), which the harness tests assert.

**No silent clamping.** PGR is reported raw. When the gap `r(M_s) − r(M_w) ≤ 0`,
or a value falls outside `[0, 1]`, the affected metric is emitted as `invalid`
with a reason in both the markdown and JSON output. This adaptation is recorded in
`scripts/router_eval.py`, its tests, and ADR 0011.

## Findings

### 1. Cascade / FrugalGPT escalation (ADOPT: cheap-first + escalate-on-signal)

FrugalGPT ([arXiv 2305.05176](https://arxiv.org/pdf/2305.05176), Chen et al.
2023) lines models up cheapest→dearest, sends the query to the cheapest first,
and runs a **generation scoring function** `g(q, a) ∈ [0,1]` on the *actual
answer*; if the score clears a per-model threshold `τ_i` the answer is returned,
otherwise the query escalates to the next model. The last model always answers.
Reported up to ~98% cost reduction vs always-GPT-4 on some tasks
([DeepWiki FrugalGPT](https://deepwiki.com/stanford-futuredata/FrugalGPT/1.2-key-concepts-and-terminology)).
The key insight vs upfront routing: the escalation decision sees *what the cheap
model actually said*, so "nothing wrong ships unexamined"
([attention.sh FrugalGPT](https://attention.sh/kb/frugalgpt/)).

- **ADOPT:** cascade = cheapest-capable first, escalate **at most once** on a
  signal. We keep it a single hop (not a full learned chain) to bound latency and
  stay dependency-free.
- **REJECT:** a trained DistilBERT reliability scorer — needs labeled data and a
  model. Instead we use **cheap, pluggable, data-driven signals** that need no
  training: `finish_reason == "length"` (truncation), empty/too-short output, and
  configurable refusal regexes. The broader prior-art survey explicitly lists
  "mid-flight escalation … cheap first, escalate on a signal" as a reusable axis
  ([forge prior-art notes](https://github.com/alp82/forge/blob/main/docs/research/complexity-routing-prior-art.md)).

### 2. Latency-aware / latency-budget routing (ADOPT)

Production routers maintain a **rolling p95 (and p50) latency window per
provider/model from their own outbound telemetry** and pick the lowest-latency
candidate that still meets cost/quality floors
([Future AGI field guide](https://futureagi.com/blog/what-is-llm-routing-2026/)).
LLMGateway weights time-to-first-token into provider scoring (and notes TTFT is
only measurable on streaming requests)
([LLMGateway routing docs](https://docs.llmgateway.io/features/routing)). Nexus
implements a `latency-budget` strategy: "the highest-quality model whose rolling
p95 latency stays within a hard SLA" — the latency dual of a cost budget
([nexus-llm-router README](https://github.com/Francis1998/nexus-llm-router/blob/main/README.md)).
Not Diamond exposes a `latency` tradeoff mode alongside quality/cost
([Not Diamond key concepts](https://docs.notdiamond.ai/docs/key-concepts)).

- **ADOPT:** a per-request `x-broker-latency-budget-ms` header (+ a config
  default). We already persist per-request `latency_ms` per served model in the
  usage log (ADR 0007), so we can compute p50/p90 over a recent window with zero
  new storage. **Cold-start aware:** add an estimated load penalty for a model
  that is not currently resident. Never override the capability filter.
- **Note:** TTFT vs end-to-end — we only record end-to-end latency, so our
  estimate is end-to-end; documented as such.

### 3. Placement / data-residency pinning, fail-closed (ADOPT, as a hard wall)

The consensus pattern: **the gateway is the enforcement point** — it classifies a
request's origin/identity, selects an endpoint from an **allow-list** for that
class, keeps **fallbacks inside the same boundary**, and **logs the decision**
([Maxim AI gateway survey](https://www.getmaxim.ai/articles/top-5-ai-gateways-for-data-residency-and-multi-region-deployments-in-2026/);
[DPDP/GDPR routing guide](https://aitechconnect.in/tips/data-residency-ai-apps-dpdp-gdpr-routing-2026)).

- **LiteLLM**: tag-based routing — deployments carry `tags: ["region:eu"]`, the
  request is stamped with a tag *by the gateway, never the end client*,
  `enable_tag_filtering: true` narrows the candidate pool to deployments sharing a
  tag, and ordinary load-balancing/retries operate **only within that pool**. The
  older `region_name`/`allowed_model_region` mechanism is now deprecated in favor
  of tags. Crucially, **"since the router filters out models not in the specified
  region, it will return back as an error … if no models in that region are
  available"** — fail-closed by construction
  ([LiteLLM tag routing](https://docs.litellm.ai/docs/proxy/tag_routing);
  [LiteLLM region routing (deprecated)](https://docs.litellm.ai/docs/proxy/customer_routing)).
- **Portkey**: conditional routing on `metadata.*` (e.g. `user_region == "EU"` →
  EU target) with a `default` target; residency/sensitivity expressed as metadata
  conditions, evaluated at the edge
  ([Portkey conditional routing](https://portkey.ai/docs/aigw/product/ai-gateway/conditional-routing)).
- The "constraint/capability routing" card frames placement as a **pre-filter**
  alongside context-window and modality: "filter out the models that *cannot
  serve the request at all* … or must stay in a specific jurisdiction" *before*
  asking which is best
  ([LLMRouting constraint card](https://github.com/pastorsj/LLMRouting/blob/main/cards/inter-model/constraint-capability-routing.md)).

- **ADOPT:** per-model `placement` labels in the registry; a request requirement
  from `x-broker-placement` (and a config default); candidates/pool members are
  filtered to those satisfying **all** required labels, for `auto`, explicit
  models, **and** pool members. If nothing qualifies → structured `403
  placement_denied`; **never** spill to a non-compliant model. This matches our
  existing "no silent fallback" posture and fail-closed security stance. The
  requirement is set server-side (header from a trusted caller / config), mirroring
  LiteLLM's "tag set by your gateway, never by the end client".

### 4. Learned / semantic routers (REJECT as a dependency; adopt lessons)

- **RouteLLM** `mf` (matrix factorization on Chatbot Arena preference data) is the
  reference learned router; strong results but needs preference data, a trained
  model, and threshold calibration ([lm-sys/RouteLLM](https://github.com/lm-sys/RouteLLM)).
- **vLLM Semantic Router** embeds queries and classifies intent with a
  ModernBERT/LoRA stack in Rust/Candle behind Envoy `ext_proc`; its newer
  Signal–Decision architecture separates *signals* (keyword, regex, embedding,
  BERT) from *decisions* composed with Boolean AND/OR
  ([vLLM SR signal-decision](https://vllm.ai/blog/2025-11-19-signal-decision);
  [When to Reason, arXiv 2510.08731](https://arxiv.org/html/2510.08731v1)). We
  already have the cheap end of this (keyword/tag + optional tiny LLM); the heavy
  embedding stack is out of scope for a local broker.
- **Not Diamond / Martian** are closed decision layers; Not Diamond explicitly
  "is not a gateway" and integrates on top of one, offering quality/cost/latency
  tradeoff modes ([Not Diamond](https://docs.notdiamond.ai/docs/what-is-model-routing);
  [comparison](https://futuretweets.com/openrouter-vs-martian-vs-not-diamond-llm-router-2026/)).
  Lesson adopted: expose cost **and latency** as explicit, per-request tradeoffs.

## Adopt / reject summary

| Item | Decision | Why |
|------|----------|-----|
| Cheap-first cascade, escalate ≤1 hop | **ADOPT** | Biggest cost lever; fits cost_tier metadata; bounded latency. |
| Trained reliability scorer (DistilBERT) | **REJECT** | Needs labeled data + a model; use pluggable signals instead. |
| Escalation signals: length / empty / refusal | **ADOPT** | Zero-dependency, data-driven, extensible. |
| Latency budget from usage-log p50/p90 | **ADOPT** | We already store per-model latency (ADR 0007); no new storage. |
| Cold-start penalty in the estimate | **ADOPT** | Lazy-load means an unloaded model is much slower first call. |
| Placement pinning, fail-closed 403 | **ADOPT** | Matches our no-silent-fallback + fail-closed security posture. |
| Placement requirement from end-client | **REJECT** | Set server-side (trusted header/config), per LiteLLM guidance. |
| RouteLLM PGR / APGR / CPT metrics | **ADOPT** | Precise, standard, computable offline from labels. |
| Learned router as a dependency (mf, BERT/LoRA) | **REJECT** | Too heavy for a local-first broker; keep heuristic + tiny GBNF. |
| Full streaming cascade | **REJECT (defer)** | Can't rescore a stream cheaply; stream falls back to single. |

## Open questions / deferred

- **TTFT vs end-to-end latency**: we only log end-to-end; a TTFT estimate would
  need adapter-level instrumentation (deferred).
- **Per-key placement policy**: we support a request header + a global config
  default now; a per-API-key placement policy map rides on the key-governance work
  in ADR 0008 (deferred, noted in roadmap).
- **Calibrated thresholds**: our cascade signals are boolean, not a calibrated
  continuous score; a learned scorer is deferred.

## Sources

1. RouteLLM paper — https://arxiv.org/pdf/2406.18665 (PGR/APGR/CPT definitions).
2. lm-sys/RouteLLM repo — https://github.com/lm-sys/RouteLLM (threshold calibration, `mf`).
3. FrugalGPT paper — https://arxiv.org/pdf/2305.05176 (LLM cascade, scoring fn, thresholds).
4. FrugalGPT key concepts (DeepWiki) — https://deepwiki.com/stanford-futuredata/FrugalGPT/1.2-key-concepts-and-terminology.
5. FrugalGPT illustrated — https://attention.sh/kb/frugalgpt/ (cascade intuition, per-rung τ).
6. LiteLLM tag-based routing — https://docs.litellm.ai/docs/proxy/tag_routing.
7. LiteLLM region routing (deprecated, fail-closed note) — https://docs.litellm.ai/docs/proxy/customer_routing.
8. DPDP/GDPR residency routing guide — https://aitechconnect.in/tips/data-residency-ai-apps-dpdp-gdpr-routing-2026.
9. Maxim AI — AI gateways for data residency — https://www.getmaxim.ai/articles/top-5-ai-gateways-for-data-residency-and-multi-region-deployments-in-2026/.
10. Portkey conditional routing — https://portkey.ai/docs/aigw/product/ai-gateway/conditional-routing.
11. LLMRouting constraint/capability card — https://github.com/pastorsj/LLMRouting/blob/main/cards/inter-model/constraint-capability-routing.md.
12. Future AGI — LLM routing field guide (latency p95 window) — https://futureagi.com/blog/what-is-llm-routing-2026/.
13. LLMGateway routing (TTFT weighting) — https://docs.llmgateway.io/features/routing.
14. nexus-llm-router (`latency-budget` strategy) — https://github.com/Francis1998/nexus-llm-router/blob/main/README.md.
15. vLLM Semantic Router — Signal-Decision — https://vllm.ai/blog/2025-11-19-signal-decision.
16. "When to Reason: Semantic Router for vLLM" — https://arxiv.org/html/2510.08731v1.
17. Not Diamond — model routing / key concepts — https://docs.notdiamond.ai/docs/what-is-model-routing.
18. CORAL (CPT 95/80 usage) — https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2026.1891737/full.
19. Dynamic Model Routing & Cascading survey — https://arxiv.org/html/2603.04445v3.
