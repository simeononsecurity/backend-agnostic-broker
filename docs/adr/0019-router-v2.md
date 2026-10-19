# ADR 0019: Router v2 — TTFT estimation, calibrated cascade scorer, streaming cascade, code/math accuracy

- Status: Accepted
- Date: 2026-10-19
- Supersedes: none (extends ADR 0011, which extends ADR 0004)
- Related: `docs/auto-router.md`, `docs/research/adr-0019-router-v2.md`,
  ADR 0011 (auto-router evolution), ADR 0007 (usage/decision store); prior art:
  FrugalGPT (reliability scorer + thresholds), IETF LLM benchmarking terminology
  (TTFT), zeph-llm / E.D.D.I (buffer-then-stream cascade).

## Context

ADR 0011 shipped the `auto` router's cascade / latency-budget / placement / eval
machinery but **deferred four items**, and the eval harness it introduced exposed
two concrete weaknesses it could not yet fix:

- The latency estimate was **end-to-end only**. For a streaming client the latency
  that matters is **time-to-first-token (TTFT)**, which is distinct from end-to-end
  latency (the latter grows with output length; TTFT does not). We recorded no TTFT.
- Cascade escalation used **fixed boolean signals** (`finish_reason == "length"`,
  empty/short, refusal regex). FrugalGPT's win comes from a *calibrated* reliability
  score with a tunable threshold; we had no such knob.
- A **streaming** cascade request fell back to the single strong pick, because a
  live stream cannot be re-scored. Whether a true streaming cascade is worth it was
  left open.
- The heuristic under-served **code/math/reasoning**: the cheap-model cost bonus
  could outweigh a single capability match, a greedy `"write a"` creative keyword
  mis-tagged code prompts, and keyword matching did not fire on natural prompts that
  shared no word with the lists. (A first cut of this ADR tried to fix this by
  growing the keyword lists, which **over-fit** — see Decision item 4.)

The tension is unchanged from ADR 0011: add routing intelligence **without** a new
mandatory dependency, **without** changing default `auto` behavior, and **without**
weakening the no-silent-fallback / fail-closed invariants. The research note
(`docs/research/adr-0019-router-v2.md`, ≥10 cited primary/industry sources) surveys
the levers; its adopt/reject table backs the decisions below.

## Decision

We resolve all four deferred items; every one is **implemented + tested**, none is
left pending, and the two we only partly adopt are explicitly bounded here.

1. **TTFT estimation (implemented).** The usage log gains a backward-compatible
   `ttft_ms` column (additive `ALTER TABLE` migration applied in place to an
   existing `usage.sqlite3`; old rows keep NULL). Streaming endpoints record the
   time to the first emitted chunk. `UsageLog.latency_percentiles(metric=...)`
   summarizes either `latency_ms` (end-to-end) or `ttft_ms`. The latency-budget
   router uses the **TTFT** estimate for a **streaming** request and the
   **end-to-end** estimate otherwise; a model with no streamed samples simply has
   no TTFT estimate and is not ranked by it. TTFT changes nothing unless a request
   carries a latency budget, so defaults reproduce v0.5.0.

2. **Calibrated reliability scorer (implemented, opt-in; trained scorer rejected).**
   A `router.cascade.scorer` block (default `enabled: false`) reduces the cheap
   hop's answer to one reliability score in `[0, 1]`: a weighted mean of **graded**
   features (answer-length adequacy, and mean token-logprob confidence when the
   response carries logprobs) with **hard safety gates** (a refusal match or a
   truncation caps the score to 0). The hop escalates when `score < threshold`,
   giving operators **one calibrated knob** (slide the threshold to demand more
   certainty) that the fixed booleans cannot express, while preserving the hard
   refusal/truncation behavior. When disabled (the default) the v0.5.0 booleans are
   used unchanged. We **reject** FrugalGPT's *trained* DistilBERT scorer: it needs
   labeled (query, answer, correct?) data and a bundled model — the same dependency
   ADR 0011 rejected — and we do not fabricate an accuracy claim we cannot measure.

3. **Buffered streaming cascade (implemented, opt-in; first-N-token commit rejected).**
   `router.cascade.stream_mode: fallback_single | buffer` (default `fallback_single`
   = v0.5.0). In `buffer` mode the **chat** endpoint serves the cheap hop
   **non-streamed**, scores it with the same `should_escalate` signals, then either
   **replays** the buffered answer as a stream or **streams the stronger hop live** —
   so **only one model's output is ever emitted into a stream, never a mix**. The
   completions/responses streaming endpoints keep `fallback_single`. We **reject**
   the first-N-token-commit alternative: a cheap model can open confidently and
   derail later, after the broker has committed to streaming it.

4. **Heuristic code/math/reasoning accuracy via structural features (implemented).**
   Keyword lists memorize vocabulary and generalize poorly: an earlier cut of this
   ADR grew the `code`/`math` word lists and normalized the cost term until the
   bundled train and author-written held-out splits scored a **perfect 1.000** — but
   an independent 44-prompt set the author never saw scored only **code 0.42 /
   math 0.10 / reasoning 0.00**. That gap is over-fitting, and the "perfect"
   numbers were not real. The correction: the `code`/`math` vocabularies are
   **reverted to the v0.5.0 lists** (the one deletion is the over-broad `"write a"`
   creative token, a generic instruction prefix that mis-stole code prompts), and
   generalization is carried by **structural features** that read the *shape* of a
   request, not its exact words (`broker/router.py`): code-like punctuation / file
   extensions / CLI-tool & language names / error shapes; digits-with-operators,
   fractions, and `prime`/`roots`/`sum of`/probability phrasing for math; and
   `why`/`prove`/`plan`/`trade-off`, syllogisms, and long multi-constraint questions
   for reasoning. Each feature has a unit test over prompts in no eval set. The
   labeled sets are reported **three ways by independence** — 82 train (in-sample),
   24 author held-out (same author/commit/vocabulary — **not** independent), and a
   **frozen 44-prompt independent** set written by the orchestrator
   (`tests/data/router_eval_independent.jsonl`, attributed). On the independent set,
   v0.5.0 → now: **code 0.25 → 0.92, math 0.10 → 0.90, reasoning 0.00 → 1.00**, with
   chat/creative unchanged at 1.0; train/independent are now close (0.98 / 0.96),
   the signature of real generalization rather than memorization. The in-sample PGR
   is **0.97 — strictly below the oracle ceiling** (the guard the overfit cut had to
   loosen). The `cost_normalized` term is retained **as an opt-in** (`router.weights
   .cost_normalized`, default **false** = v0.5.0) because it made **no difference on
   the independent set** — it only lifted the author's in-sample numbers. Honest
   caveat recorded in the docs: a keyword+structural heuristic generalizes only
   partially; for reliable code/math/reasoning routing prefer the LLM router backend
   or the calibrated cascade. The cost sweep still traces the full curve.

No new mandatory dependency is introduced; all new behavior is opt-in and
default-off, and a request with none of the new knobs reproduces v0.5.0 exactly.

## Alternatives considered

- **Trained reliability scorer (FrugalGPT `g(q,a)`)** — best escalation accuracy,
  but a trained model + labeled data + a new dependency; rejected for a local-first
  broker (ADR 0011 rejected it too). We adopt the *threshold* idea with a cheap,
  honest aggregator instead.
- **First-N-token streaming cascade** — lower perceived TTFT than full buffering,
  but commits to a cheap answer that may derail mid-stream. Rejected in favor of
  buffer-then-stream, whose cost (kept path pays the cheap model's full generation
  as TTFT) is documented and opt-in.
- **Streaming cascade for completions/responses** — those endpoints transform the
  chat SSE into other formats; buffering across those layers adds complexity for
  little gain, and `fallback_single` already upholds the never-mix invariant there.
  Rejected (chat-only buffering).
- **Changing default `router.weights` / normalizing the cost term by default** to
  fix code/math — the first cut of this ADR did exactly this (normalized cost on by
  default). On the independent set it changed **nothing** (identical accuracy); it
  only lifted the author's in-sample numbers and silently altered v0.5.0 default
  behavior. Rejected as the default; kept as an **opt-in** `cost_normalized` knob
  (default false = v0.5.0). Generalization is carried instead by structural
  features, which move the independent-set numbers.
- **Status quo (end-to-end latency, booleans, no streaming cascade)** — leaves the
  streaming latency signal wrong, the escalation un-tunable, and streaming cascades
  unserved. Rejected.

## Consequences

- **Positive** — streaming requests are judged on the latency they actually
  perceive (TTFT); cascade escalation gains a single calibrated threshold (+ an
  optional logprob signal); a streaming cascade can now run end-to-end without ever
  mixing two models; code/math/reasoning routing improves on an **independent**
  prompt set (code 0.25→0.92, math 0.10→0.90, reasoning 0.00→1.00), measured — not
  on the author's own held-out split, which is not independent.
- **Negative** — the buffered streaming cascade pays the cheap model's full
  generation time as perceived TTFT on the kept path (inherent to buffering); the
  reliability scorer is a heuristic aggregator, not a trained/calibrated regressor,
  so its benefit is operational (a tunable knob), not a measured accuracy gain; the
  capability-first heuristic spends slightly more (mean cost_tier 2.57→2.83 on
  train) to stop under-serving code/math; and a keyword+structural heuristic still
  generalizes only partially — honest, not perfect, so the docs point operators to
  the LLM router backend / cascade for hard routing.
- **Risks** — a mis-set scorer threshold could over- or under-escalate (mitigation:
  opt-in, default off, threshold validated to `[0,1]`, hard gates keep refusal/
  truncation safety); a buffered cascade could stall a client on a very long cheap
  answer (mitigation: opt-in, documented, chat-only); **over-fitting the heuristic
  to the eval prompts — which the first cut of this ADR did** (train/held-out 1.0,
  independent 0.43). Mitigation adopted here: a frozen **independent** set written
  by a different author is committed and enforced disjoint from train and the author
  held-out split; acceptance is pinned on *that* set (code ≥ 0.6, math ≥ 0.5,
  reasoning ≥ 0.3) and the in-sample PGR is asserted **< 1.0** (not saturated); the
  features are structural/general, not per-prompt.

## Confirmation

Implemented in `broker/usage.py` (`ttft_ms` column + migration +
`latency_percentiles(metric=...)`), `broker/router.py` (`reliability_score`,
scorer-aware `should_escalate`, `stream`-aware latency estimate, **structural
code/math/reasoning features** + reverted keyword lists + opt-in `cost_normalized`),
`broker/registry.py` (`CascadeConfig.stream_mode`, `ScorerConfig`,
`RouterWeights.cost_normalized`), `broker/manager.py` (`StreamPlan`, `plan_stream`,
`stream_cascade_buffer`, SSE replay), and `broker/app.py` (TTFT capture on all
streaming endpoints; chat wires the buffered cascade). Verified by:

- `tests/test_router_v2.py` — TTFT recorded + **old-schema DB migration**, TTFT
  metric excludes non-stream rows, **latency budget uses TTFT for streaming** vs
  end-to-end otherwise, **scorer threshold behavior** (monotone knob, hard gates,
  logprob feature, disabled-preserves-booleans), **buffered streaming cascade never
  mixes two models** (kept-replay, escalate-stream, and a dedicated end-to-end
  no-mixing test asserting only the final model's tokens + the `x-broker-route`
  header), completions stays fallback_single, **per-structural-feature unit tests**
  over prompts in no eval set, the **independent set frozen/disjoint** from train
  and author held-out, **honest three-way pins** (in-sample not saturated; on the
  independent set code ≥ 0.6, math ≥ 0.5, reasoning ≥ 0.3; chat/creative ≥ v0.5.0),
  and the opt-in `cost_normalized` default (false) + behavior.
- Existing `tests/test_router_cascade.py`, `tests/test_router_latency.py`,
  `tests/test_router_eval.py`, `tests/test_usage.py`, `tests/test_streaming.py` stay
  green (default behavior preserved). The eval PGR assertion is kept at the v0.5.0
  bound **`0.0 ≤ PGR < 1.0`** (the overfit cut had loosened it to `≤ 1.0`); the
  honest heuristic does not saturate, so the original bound holds and the v0.5.0
  back-compat suite passes unmodified.

The honest three-way eval tables (train / author held-out / independent, before and
after) are reproduced in `docs/auto-router.md`.
