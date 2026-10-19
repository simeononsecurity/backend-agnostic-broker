# Research note — ADR 0019: router v2 (TTFT, calibrated cascade, streaming cascade, code/math)

> Status: input to ADR 0019. All external pages below were fetched as **untrusted
> data** and are cited, not obeyed; none contained agent-directed instructions
> that were followed. Confidence is stated per decision.

## Question

Resolve the four items ADR 0011 deferred:

1. Estimate **time-to-first-token (TTFT)** and let the latency-budget router use it
   for streaming requests (vs end-to-end for non-stream).
2. Replace/augment the fixed cascade escalation booleans with a **calibrated
   reliability scorer** — only if research + eval show benefit, else reject.
3. Decide on a **full streaming cascade** (buffer-then-stream, or reject).
4. Measurably improve the heuristic router on its **weak task types** (code, math,
   reasoning), verified on a **frozen independent** split, not just the author's own.

## Sources (primary first)

1. IETF, *Benchmarking Terminology for LLM Serving* (draft-gaikwad-llm-benchmarking-terminology-00) —
   TTFT = "elapsed time between request initiation and receipt of the first output
   token"; **"For non-streaming responses, TTFT equals end-to-end latency because
   all tokens arrive together."** TTFT includes queue + prefill; E2E grows with
   output length. <https://www.ietf.org/archive/id/draft-gaikwad-llm-benchmarking-terminology-00.html>
2. Modular, *Key metrics for LLM inference* — TTFT vs TPOT vs E2EL; SLOs like
   "TTFT ≤ 200 ms and E2EL ≤ 3000 ms"; "a fast TTFT followed by slow token
   generation still leads to a poor experience." <https://handbook.modular.com/llm-inference-basics/llm-inference-metrics/>
3. Parasail, *How to reduce LLM latency in production* — "Routing is one of the
   most important layers above the engine for TTFT"; E2E ≈ TTFT + TPOT·tokens, so
   E2E grows with output length while TTFT does not. <https://www.parasail.io/blog/how-to-reduce-llm-latency-in-production>
4. Tencent Cloud, *LLM API Observability* — use **p95 TTFT for streaming chat**,
   p95 TPOT for generation; "a role event, heartbeat, or metadata-only chunk is
   not the first content token." <https://www.tencentcloud.com/techpedia/147975>
5. Chen, Zaharia, Zou, *FrugalGPT* (arXiv:2305.05176), via DeepWiki — the LLM
   **cascade**: cheapest model first, a **Scorer** (small DistilBERT regressor
   trained on labeled (query, answer, correct?) data) predicts answer correctness,
   and a learned **per-model threshold** decides keep-vs-escalate; thresholds +
   ordering are fit jointly under an average-cost budget. <https://deepwiki.com/stanford-futuredata/FrugalGPT/1.2-key-concepts-and-terminology>
6. *FrugalGPT, illustrated* (attention.sh) — "the reliability scorer … returns a
   number between 0 and 1"; each rung has a threshold τ; "slide the threshold up
   and you demand more certainty — fewer answers kept, more escalated"; "a
   calibrated *abstain* (escalate on doubt) beats a clever accept." <https://attention.sh/kb/frugalgpt/>
7. Ironclad Academy, *Model routing and cascades* — **"Cascades don't stream."**
   Two options: (a) **buffer** the full tier-one response, check confidence, then
   release or escalate — every query pays the tier-one generation time as perceived
   TTFT; (b) check confidence on the first N tokens and commit to streaming — better
   TTFT but "a model can open confidently and derail at token 50, and you have
   already committed." <https://www.ironclad.academy/ai-engineering/articles/llm-model-routing-cascades-production>
8. E.D.D.I, *Model Cascade* docs — a **guaranteed-accept step is streamed live**;
   "any step that could still escalate is always **buffered** instead (its full
   text is needed to evaluate confidence before deciding)." <https://docs.labs.ai/agent-configuration/model-cascade>
9. Vibe Engines, *Design an LLM Router* — buffering "pushes time-to-first-token out
   to the cheap model's *full* completion time on every request in order to protect
   the minority that will fail it." <https://vibeengines.com/ai-system-design/llm-router-system-design>
10. `zeph-llm` `router::cascade` (Rust, docs.rs) — production pattern: cascade
    `chat_stream` **buffers the cheap provider's full response** to classify it; on
    non-escalation the buffered response is **replayed as a single stream chunk**
    (no extra latency), on escalation the stronger provider streams; a cumulative
    **token budget** caps runaway cost. <https://docs.rs/zeph-llm/latest/zeph_llm/router/cascade/index.html>

## Findings → decisions

| Item | Finding (sources) | Decision (ADR 0019) | Confidence |
|------|-------------------|----------------------|------------|
| TTFT estimation | TTFT is the latency a streaming client perceives; it is distinct from E2E, which grows with output length; routing drives TTFT; for non-stream TTFT≡E2E (1–4) | **Adopt.** Record `ttft_ms` for streamed requests (backward-compatible column + migration); the latency budget uses the **TTFT** p90 for streaming requests and **end-to-end** p90 for non-stream. | High |
| Calibrated scorer | A *trained* DistilBERT scorer with learned per-model thresholds is the proven win, but needs labeled data + a model (5,6) — already rejected as too heavy in ADR 0011. The *threshold* idea (one knob; "escalate on doubt") is cheap and valuable (6) | **Adopt a lightweight form, reject the trained form.** Implement an opt-in reliability score = graded features (length adequacy + optional token-logprob confidence) with **hard refusal/truncation gates** and one tunable `threshold`; keep the booleans as the default. Reject the trained DistilBERT regressor (no labeled data; local-first; dependency). | Medium |
| Streaming cascade | Every source converges on **buffer-then-stream**: buffer any step that can still escalate, stream only a guaranteed-accept step; replay the buffered cheap answer as one chunk on the non-escalation path; the first-N-token-commit alternative can derail mid-answer (7–10) | **Adopt buffer-then-stream (opt-in), reject first-N commit.** Chat endpoint only; completions/responses keep `fallback_single`. Invariant: one model's output per stream, never mixed. | High |
| Code/math heuristic | n/a (internal) — root cause was the cost bonus rivaling a capability match, a greedy `"write a"` creative keyword, and keyword lists that do not fire on natural prompts | **Adopt structural features; reject growing the keyword lists.** A first attempt grew the `code`/`math` vocabularies and normalized the cost term by default — it scored a *false* perfect 1.0 on the author's own held-out split but only code 0.42 / math 0.10 / reasoning 0.00 on an independent set (over-fitting). Corrected: revert the vocabulary to v0.5.0 (minus the over-broad `"write a"`), carry generalization with **structural features** (punctuation / file-ext / CLI & language names / error shapes; operators & math phrasing; argument/puzzle/planning structure), gate `cost_normalized` as an opt-in (it did not move the independent set), and pin accuracy on a **frozen independent** set written by a different author. | Medium |

## Why not the alternatives

- **Trained reliability scorer (FrugalGPT `g(q,a)`)** — best accuracy, but a trained
  model + labeled preference data + a new dependency; contradicts the local-first,
  no-new-mandatory-dependency invariant. Our heuristic scorer is honest about being
  an aggregator, not a trained regressor, and is **opt-in**.
- **First-N-token streaming cascade** — better TTFT than full buffering, but a cheap
  model can "open confidently and derail at token 50" after you have committed to
  streaming it (7). We refuse to risk shipping a bad cheap answer; buffering is the
  safe choice and the cost (perceived TTFT = cheap model's full generation on the
  kept path) is documented and opt-in.
- **Changing default `auto` behavior for latency** — rejected; TTFT only affects a
  request that *sets* a latency budget, so defaults reproduce v0.5.0.

## Honesty / limitations

- The router-eval "accuracy" is **capability-acceptability** on a synthetic 6-model
  catalogue, not human-graded answer quality. Results are reported **three ways by
  independence** — 82 train (in-sample), 24 author held-out (same commit/vocabulary,
  **not** independent, saturates), and a **frozen 44-prompt independent** set written
  by a different author. The honest signal is the **independent** column: code
  0.25→0.92, math 0.10→0.90, reasoning 0.00→1.00, with chat/creative unchanged and
  in-sample PGR kept **< 1.0** (not saturated). A keyword+structural heuristic
  generalizes only **partially**; for reliable hard routing use the LLM router
  backend or the calibrated cascade. An earlier cut of this work reported a
  "perfect" 1.0 on the author's held-out split that did **not** hold on independent
  prompts (train 1.0 / independent 0.43) — that over-fit is the mistake this note
  and ADR 0019 exist to correct.
- The reliability scorer's "benefit" is **operational** (one calibrated threshold +
  optional logprob signal; a monotone escalation knob the fixed booleans cannot
  express), demonstrated by a threshold-sweep test — not a measured accuracy gain,
  which would require labeled (query, answer, correct?) data we deliberately do not
  add.
