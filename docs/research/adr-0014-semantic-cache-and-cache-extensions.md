# Research note — ADR 0014: semantic cache, streaming replay, cache controls, opt-in span content

- Date: 2026-10-19
- For: ADR 0014 (resolves every ADR 0012 cache/observability deferral)
- Confidence: **High** on thresholds and OTel content-capture conventions
  (multiple converging primary sources); **Medium** on the exact false-positive
  numbers (vendor-measured on their own workloads, not reproduced here).

> All sources below were fetched as **untrusted data**. None contained
> agent-directed text or prompt-injection; they are cited as evidence only.

## Sub-questions

1. What cosine-similarity threshold do production semantic caches default to, and
   what is the false-positive (wrong-answer) risk by threshold?
2. How is a cached response safely replayed for a streaming (`stream: true`)
   request as OpenAI-compatible SSE?
3. What per-request cache controls (TTL, namespace, `Cache-Control`) are
   well-founded, and which should we reject?
4. How do the OpenTelemetry GenAI conventions capture prompt/response content,
   and what is the safe default?

## 1. Semantic-cache similarity threshold

| Source | Default / recommended | Notes |
|--------|----------------------|-------|
| Redis **RedisVL** `SemanticCache` [R1] | `distance_threshold=0.1` COSINE distance ⇒ **≈0.90 cosine similarity** | distance [0–2], lower = stricter |
| **LiteLLM** semantic cache [R2][R3] | `similarity_threshold: 0.8` | same knob across Redis/Valkey/Qdrant backends; scope isolated per key (`semantic_cache_scope`) |
| **GPTCache** / spheron guide [R4] | **0.92** start for factual; "danger zone 0.88–0.94" | "Set threshold ≥0.92 for factual/RAG; below 0.90 too many false hits" |
| prem.ai guide [R5] | **0.90** recommended default | 0.85 catches paraphrases but risks FP; 0.95 safer, lower hit rate |
| respan.ai measured table [R6] | **0.97 for first deployment**; 0.93 "balanced" | FP by threshold: 0.99→<0.1%, **0.97→~0.5%, 0.95→1–3%, 0.93→3–7%, 0.90→7–15%, 0.85→15–30%** |
| arXiv 2411.05276 [R7] | 0.8 (empirically tuned on one validation set) | 97%+ positive-hit accuracy at 0.8 on *their* data |

**Decision — default `BROKER_SEMANTIC_CACHE_THRESHOLD = 0.95`.** The broker is a
**multi-tenant** gateway: a false positive means serving one caller a different
caller's (or a different question's) answer — a *correctness and trust* failure,
not just a quality dip. Unlike a single-app deployment we have **no built-in
per-workload eval loop** to tune against, so we err toward precision. respan's
measured table [R6] puts 0.95 at ~1–3% FP vs 3–7% at 0.93; we take the safer band
while staying well above 0.99 (which almost never hits). Operators who have run an
eval loop can lower it. We document the FP/threshold tradeoff and that strict
scoping (below) removes the cross-key/model/param class of false hits entirely —
only *prompt-text* fuzziness remains.

Cosine is computed in **pure Python** (dot / norms) over a **bounded, scoped,
brute-force** bucket — no numpy, matching "no mandatory numpy." RedisVL/LiteLLM
use a real ANN index; at our per-bucket entry counts (default 128) brute force is
fine and dependency-free [R1][R2].

## 2. Streaming cache replay

OpenAI streaming is **data-only SSE**: each event is `data: {json}\n\n`, the JSON
is a `chat.completion.chunk` with a `choices[].delta` holding a role token, a
content token, or `{}` at end, terminated by `data: [DONE]\n\n` [S1][S2][S3][S4].
The Responses API emits its own event types (`response.output_text.delta`,
`response.completed`) [S4][S5].

**Decision.** On a cache **hit** for `stream: true`, synthesize a well-formed SSE
replay from the stored non-stream result: a role-delta chunk, a content-delta
chunk carrying the full text, a finish chunk, then `[DONE]`. The broker already
has `chat_sse_to_text_sse` / `chat_sse_to_responses_sse`, so we build the chat
replay once and pass it through those converters for `/v1/completions` and
`/v1/responses` — guaranteeing the three endpoints stay format-correct by reusing
the exact production translators. We **only populate** the cache from
fully-successful **non-streaming** responses (as today); streaming requests read
but never write, so an assembled-stream is never stored.

## 3. Per-request cache controls (RFC 9111)

RFC 9111 [C1][C2]: `Cache-Control` directives are **unidirectional** — a request
directive does not imply a response directive. The **`max-age` request directive**
is a *client freshness preference* (`max-age=5`, token form, no quotes).
**`s-maxage` is a response directive for shared caches** and presupposes stored
**age tracking + revalidation (304/ETag)** [C3][C4] — machinery the broker does
not have (it stores whole responses with a TTL and has no conditional-request
protocol).

**Decision.**
- **Adopt** `x-broker-cache-ttl: <seconds>` — overrides the write TTL for *this*
  request's stored entry (bounded, validated ≥0). Clean analogue to a per-item
  TTL; no revalidation needed.
- **Adopt** `x-broker-cache-namespace: <token>` — folded into the cache *scope*
  (after key/tenant + placement) so a caller can partition or cheaply invalidate
  their own cache by rotating the namespace. Matches LiteLLM's `namespace`
  cache-param [R3].
- **Map** request `Cache-Control: max-age=0` to a **bypass** (strictest reading,
  consistent with the existing `no-cache`/`no-store` bypass) [C1].
- **Reject** `s-maxage` and request `max-age>0` *freshness windows*: the broker
  keeps no per-entry stored-age and has no revalidation protocol, so honoring them
  would be a silent lie. TTL is the honest lever; `x-broker-cache-ttl` exposes it.

## 4. Opt-in span content capture (OTel GenAI)

The OTel GenAI conventions make content capture **off by default** because prompts
/completions carry PII [O1][O2][O3][O4][O5]. The spec's `ContentCapturingMode`:
`noContent` (default), `spanOnly` (span attributes `gen_ai.input.messages`,
`gen_ai.output.messages`, `gen_ai.system_instructions` as JSON strings),
`eventOnly` (a `gen_ai.client.inference.operation.details` log record),
`spanAndEvent` [O1][O2]. Instrumentations gate it behind
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` [O2]; an older
convention used `gen_ai.content.prompt`/`completion` events [O5].

**Decision — `BROKER_TRACING_CAPTURE_CONTENT = false` (default), spanOnly shape.**
When explicitly enabled, the `inference` span gains `gen_ai.input.messages` and
`gen_ai.output.messages` as **truncated JSON strings** (`BROKER_TRACING_CONTENT_MAX_CHARS`,
default 2000), matching the spec's `spanOnly` attribute names [O1][O3]. Never on
by default; a loud warning in docs; disabled = byte-for-byte today (the existing
`tests/test_tracing.py` asserts no span carries message content, which stays green).

## Adopt / reject summary

| Item (ADR 0012 deferral) | Decision | Rationale |
|--------------------------|----------|-----------|
| Semantic response cache | **Adopt**, opt-in, threshold 0.95, strict non-prompt scoping, resident-only embed gate | [R1–R7]; precision-first for a multi-tenant gateway |
| Semantic cache may load an extra embedding model | **Reject silent load**; consult only when embedding model already resident (documented opt-in override) | preserves the lazy-load invariant |
| Streaming cache replay | **Adopt** (read-only for streams; non-stream populates) | [S1–S5]; reuse existing SSE translators |
| `x-broker-cache-ttl`, `x-broker-cache-namespace`, `max-age=0`→bypass | **Adopt** | [C1][R3] |
| `s-maxage` / request `max-age>0` freshness | **Reject** | [C1–C4]; no age tracking/revalidation |
| Opt-in span content capture | **Adopt**, default off, spanOnly, truncated | [O1–O5] |

## Sources

- [R1] Redis RedisVL `SemanticCache` API — https://redis.io/docs/latest/develop/ai/redisvl/0.26.0/api/cache/
- [R2] LiteLLM semantic caching — https://docs.litellm.ai/docs/proxy/caching_semantic
- [R3] LiteLLM cache settings (namespace, similarity_threshold) — https://docs.litellm.ai/docs/proxy/caching_settings
- [R4] Semantic caching for LLM inference (GPTCache, threshold 0.92) — https://www.spheron.network/blog/semantic-cache-llm-inference-gpu-cloud/
- [R5] prem.ai, "Semantic caching for LLMs" (0.90 default) — https://www.premai.io/blog/semantic-caching-for-llms-how-to-cut-api-bills-by-60-without-hurting-quality/
- [R6] respan.ai, "Semantic cache: when to ship" (FP/threshold table) — https://www.respan.ai/articles/semantic-cache-llm
- [R7] arXiv 2411.05276, in-memory semantic caching (empirical 0.8) — https://arxiv.org/html/2411.05276v3
- [S1] OpenAI Chat Completions streaming events — https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events
- [S2] OpenAI streaming responses guide — https://developers.openai.com/api/docs/guides/streaming-responses
- [S3] OpenAI cookbook, how to stream completions — https://developers.openai.com/cookbook/examples/how_to_stream_completions
- [S4] OpenAI-compatible SSE chunk/`[DONE]` format — https://intl.cloud.tencent.com/document/product/1300/82345
- [S5] OpenAI-compatible endpoints (Responses stream events) — https://learn.microsoft.com/en-us/agent-framework/hosting/self-hosting/openai-endpoints
- [C1] RFC 9111 (HTTP Caching) text — https://www.rfc-editor.org/rfc/rfc9111.txt
- [C2] RFC 9111 info page — https://www.rfc-editor.org/info/rfc9111/
- [C3] Core caching fundamentals (max-age vs s-maxage) — https://www.cache-control.com/core-caching-fundamentals-http-lifecycle/
- [C4] HTTP cache hierarchy / directive scope — https://www.cache-control.com/core-caching-fundamentals-http-lifecycle/understanding-http-cache-hierarchy/
- [O1] OTel GenAI conventions, content-capture modes — https://genkit.dev/docs/observability/otel-genai-semantic-conventions/
- [O2] Greptime, how OTel traces LLM calls (capture env flag, operation.details) — https://greptime.com/blogs/2026-05-09-opentelemetry-genai-semantic-conventions
- [O3] opentelemetry.io, GenAI observability (content opt-in, span attributes) — https://opentelemetry.io/blog/2026/genai-observability/
- [O4] OTel semantic-conventions issue #2010 (content on spans as JSON) — https://github.com/open-telemetry/semantic-conventions/issues/2010
- [O5] Klu.ai, LLM session tracing (older `gen_ai.content.*` events) — https://klu.ai/glossary/llm-session-tracing-opentelemetry
