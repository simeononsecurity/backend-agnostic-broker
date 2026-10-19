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
- The heuristic measured **code 0.60** and **math 0.33** on the 45-prompt set — the
  cheap-model cost bonus could outweigh a single capability match, and a greedy
  `"write a"` creative keyword mis-tagged code prompts.

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

4. **Heuristic code/math accuracy (implemented).** The heuristic's cost term is
   **normalized** to `[0, 1]·w.cost` so at the default weights it is a true
   tie-breaker that can never outweigh a single capability match (a cheap generalist
   no longer beats the one capable model on a code/math prompt); the greedy
   `"write a"` creative keyword is removed and the `code`/`math` vocabularies are
   broadened with generically-true terms. The labeled set grows to **82 train**
   prompts (≥15 code, ≥10 math) plus a **frozen 24-prompt held-out** split
   (`tests/data/router_eval_heldout.jsonl`) that is **not used for tuning**. The
   cost sweep still traces the full cost–quality curve, so APGR/CPT remain
   meaningful.

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
- **Changing default `router.weights`** to fix code/math — simplest, but ripples the
  whole curve and the v0.5.0 behavior. Rejected in favor of a structural fix
  (normalized cost) that keeps the defaults and the sweep semantics.
- **Status quo (end-to-end latency, booleans, no streaming cascade)** — leaves the
  streaming latency signal wrong, the escalation un-tunable, and streaming cascades
  unserved. Rejected.

## Consequences

- **Positive** — streaming requests are judged on the latency they actually
  perceive (TTFT); cascade escalation gains a single calibrated threshold (+ an
  optional logprob signal); a streaming cascade can now run end-to-end without ever
  mixing two models; code/math routing improves sharply with frozen held-out
  evidence and pinned-in-tests thresholds.
- **Negative** — the buffered streaming cascade pays the cheap model's full
  generation time as perceived TTFT on the kept path (inherent to buffering); the
  reliability scorer is a heuristic aggregator, not a trained/calibrated regressor,
  so its benefit is operational (a tunable knob), not a measured accuracy gain; the
  capability-first heuristic spends slightly more (mean cost_tier 2.57→2.88 on
  train) to stop under-serving code/math.
- **Risks** — a mis-set scorer threshold could over- or under-escalate (mitigation:
  opt-in, default off, threshold validated to `[0,1]`, hard gates keep refusal/
  truncation safety); a buffered cascade could stall a client on a very long cheap
  answer (mitigation: opt-in, documented, chat-only); over-fitting the heuristic to
  the eval prompts (mitigation: the frozen held-out split is disjoint from train and
  enforced by a test, and the changes are structural/general, not per-prompt).

## Confirmation

Implemented in `broker/usage.py` (`ttft_ms` column + migration +
`latency_percentiles(metric=...)`), `broker/router.py` (`reliability_score`,
scorer-aware `should_escalate`, `stream`-aware latency estimate, normalized-cost
heuristic + keyword fixes), `broker/registry.py` (`CascadeConfig.stream_mode`,
`ScorerConfig`), `broker/manager.py` (`StreamPlan`, `plan_stream`,
`stream_cascade_buffer`, SSE replay), and `broker/app.py` (TTFT capture on all
streaming endpoints; chat wires the buffered cascade). Verified by:

- `tests/test_router_v2.py` — 21 tests: TTFT recorded + **old-schema DB migration**,
  TTFT metric excludes non-stream rows, **latency budget uses TTFT for streaming**
  vs end-to-end otherwise, **scorer threshold behavior** (monotone knob, hard gates,
  logprob feature, disabled-preserves-booleans), **buffered streaming cascade never
  mixes two models** (kept-replay and escalate-stream), completions stays
  fallback_single, **held-out split frozen/disjoint**, and **pinned accuracy** on
  train and held-out (code/math ≥ 0.90 / 0.80).
- Existing `tests/test_router_cascade.py`, `tests/test_router_latency.py`,
  `tests/test_router_eval.py`, `tests/test_usage.py`, `tests/test_streaming.py` stay
  green (default behavior preserved; the eval PGR assertion relaxed to allow a
  genuine 1.0 now that the improved heuristic reaches the oracle ceiling).

Before/after eval tables (train vs frozen held-out) are reproduced in
`docs/auto-router.md`.
