# ADR 0014: Semantic cache, streaming cache replay, per-request cache controls, and opt-in span content

- Status: Accepted
- Date: 2026-10-19
- Supersedes: none
- Extends: ADR 0012 (response cache / runtime control / observability)
- Related: ADR 0009 (StateStore — the cache's backing store); ADR 0006
  (embeddings path reused for the semantic cache); ADR 0011 (placement scoping);
  `docs/roadmap.md` items 10/12; research:
  `docs/research/adr-0014-semantic-cache-and-cache-extensions.md`. Prior art:
  GPTCache, Redis RedisVL `SemanticCache`, LiteLLM semantic cache, RFC 9111,
  OpenTelemetry GenAI semantic conventions.

## Context

ADR 0012 shipped an exact response cache, admin runtime control, and
metadata-only OpenTelemetry tracing, and explicitly **deferred** four items:

1. a **semantic (embedding-similarity) response cache**;
2. **streaming cache replay** (ADR 0012 bypassed the cache for `stream: true`);
3. **cache namespace / `s-maxage`-style per-request refinements**;
4. **opt-in span content capture** (ADR 0012 captured metadata only).

This ADR resolves all four: each is either implemented + tested or explicitly
**Rejected** here with a rationale. No item is left "deferred."

The invariants from ADR 0012 still bind: **no model loads at startup or on a
health check** (lazy load); **no silent cross-model fallback**; new behavior is
**opt-in and default-off** (defaults reproduce v0.5.0); optional behavior must not
add a mandatory dependency; **no prompt/response content leaves the broker**
without explicit opt-in; and a cached answer must **never cross a key/tenant
/placement boundary**.

The semantic cache creates a genuine tension with the lazy-load invariant: a
similarity lookup must embed the prompt, which needs an embedding model — a model
the chat request did not ask for. This ADR resolves that tension explicitly
(§1, "No-silent-extra-load rule").

## Decision

Four capabilities, all **opt-in with defaults that reproduce v0.5.0**.

> **Follow-up amendment (2026-10-19, pre-merge; branch not yet pushed).**
> Orchestrator verification found three defects in the first implementation, fixed
> in place on this branch and reflected in the Decision text above and the
> Confirmation below: (1) the per-request `x-broker-cache-ttl` had no finite
> ceiling (`0`/`nan`/`inf`/`1e400` were stored without expiry, overriding the
> operator TTL) → it is now finite, `>= 0`, may only *shorten* the operator TTL,
> and `0` means "do not store"; (2) the *whole* message list was embedded, so
> differing system prompts or histories could produce a cross-context hit → only
> the **last user turn's text** is fuzzy now, everything else is exact; (3) a
> cascade that escalated to a different model was stored under the pre-resolution
> model → the cache now stores only under the **served** model. The semantic
> bucket **count** is also bounded (`BROKER_SEMANTIC_CACHE_MAX_BUCKETS`).

### 1. Semantic response cache (`BROKER_SEMANTIC_CACHE_ENABLED`, default off)

A second cache layer consulted **only after an exact-cache miss**, for chat,
completions, and responses (not embeddings — an embedding *is* the vector, so
there is no fuzzy-text benefit and the exact cache already covers it).

- **Strict scoping; only the last user turn's text is fuzzy.** The semantic index
  is partitioned by a **non-prompt bucket key** = SHA-256 of
  `(caller scope, resolved public model, endpoint, every non-prompt request
  field, the system/developer messages, and the whole conversation history)` —
  sampling params, `tools`, `tool_choice`, `response_format`, `seed`,
  `max_tokens`, etc., plus the ADR 0012 scope (key/tenant + placement + optional
  namespace), **plus every message except the text of the final user turn**
  (system prompts, earlier user/assistant/tool turns, and any non-text content
  parts of the last user turn are all exact). Two requests can share a semantic
  hit **only if every one of those matches exactly**; just the *last user turn's
  text* is compared by cosine similarity. Completions (`prompt`) and responses
  (`input` + `instructions`) are normalized to `messages` before caching, so this
  single rule covers all three endpoints — `instructions` becomes an exact system
  turn. This removes the cross-key / cross-model / cross-param / cross-system-prompt
  / cross-history false-hit class entirely by construction — the remaining risk is
  purely a paraphrase of the final user turn.
- **Cosine threshold, default `0.95`.** Chosen from the research survey (Redis
  ≈0.90, LiteLLM 0.80, GPTCache 0.92, respan measured ~1–3% false-positive at
  0.95 vs 3–7% at 0.93). The broker is a **multi-tenant gateway with no built-in
  eval loop**, and a false positive serves a *wrong* answer, so we default to the
  precision-favoring band and document how to lower it. Cosine is computed in
  **pure Python** over a bounded, per-bucket, brute-force list — **no numpy**,
  no new dependency.
- **No-silent-extra-load rule (invariant resolution).** The broker **consults /
  populates the semantic cache only when the configured embedding model is
  already resident** (`is_active()` in the broker's lifecycle, i.e. a prior
  embeddings request or an admin preload warmed it). If it is not resident, the
  request is a clean **miss** and **no embedding is computed and no model is
  loaded** — a chat request can never trigger an embedding-model load. An
  operator who wants the cache always-warm preloads the embedding model
  (`POST /admin/models/{alias}/load`) or sends it traffic. `BROKER_SEMANTIC_CACHE_REQUIRE_RESIDENT=false`
  is the **explicit, documented opt-in exception** that permits on-demand
  embedding-model loading (off by default). The embedding model is an ordinary
  registry alias served through the broker's own `/v1/embeddings` path (ADR 0006)
  — no second embedding stack.
- **Deterministic-only by default** (shares `BROKER_RESPONSE_CACHE_DETERMINISTIC_ONLY`).
- **Backed by the ADR 0009 StateStore** (`kv_*`): per-bucket JSON list of
  `(embedding, response)` entries, bounded by `BROKER_SEMANTIC_CACHE_MAX_ENTRIES`
  (default 128, per bucket) **and the number of buckets bounded by
  `BROKER_SEMANTIC_CACHE_MAX_BUCKETS`** (default 1024, LRU) so a caller varying
  non-prompt params cannot grow the keyspace without limit, each with its own TTL
  (`BROKER_SEMANTIC_CACHE_TTL_SECONDS`). In-process by default, Redis when
  configured (shared across replicas).
- **Store only under the served model.** The bucket is keyed on the model resolved
  *before* serving; a cascade may escalate to a different model. The response is
  stored **only when the served model equals the keyed model** — otherwise the
  store is skipped and counted (`skipped_model_mismatch`), so a later request that
  resolves to the primary is never served an answer a different model produced.
- **Header.** A semantic hit returns `x-broker-cache: semantic-hit` (distinct
  from the exact `hit`), plus `x-broker-cache-similarity: <score>`.
- **Never across scopes.** Enforced by the bucket key; tested with different
  keys, placements, and sampling params.

### 2. Streaming cache replay

`stream: true` requests now **consult** the cache (exact, then semantic) and, on
a hit, return a **well-formed SSE replay** of the stored result instead of
bypassing. The replay is assembled as `chat.completion.chunk` events (role delta,
content delta, finish chunk, `data: [DONE]`) and passed through the existing
`chat_sse_to_text_sse` / `chat_sse_to_responses_sse` translators so
`/v1/completions` and `/v1/responses` stay format-correct by reusing the exact
production code. **Only fully-successful non-streaming responses populate the
cache** (as in ADR 0012); a streaming request reads but never writes, so an
assembled stream is never stored. Streaming responses now also carry the
`x-broker-cache` header. On a miss, the streaming path is byte-for-byte ADR 0012.

### 3. Per-request cache controls

- **Adopt `x-broker-cache-ttl: <seconds>`** — clamps the write TTL for this
  request's stored entry. It must be **finite** and `>= 0` and may only **shorten**
  the operator TTL (`BROKER_RESPONSE_CACHE_TTL_SECONDS` /
  `BROKER_SEMANTIC_CACHE_TTL_SECONDS`): the effective TTL is `min(request, operator)`
  when the operator TTL is finite, else the request value (a finite request TTL
  still bounds an otherwise-unbounded operator entry). `0` means **do not store this
  response** (never "store forever"); non-finite values (`nan`/`inf`/`-inf`, and
  overflowing literals such as `1e400`) and negatives are rejected with `400`. A
  client therefore can never store a non-expiring entry or extend one beyond the
  operator's ceiling, and every Redis write carries a finite TTL whenever the
  operator TTL is finite.
- **Adopt `x-broker-cache-namespace: <token>`** — folded into the cache scope
  (after key/tenant + placement) so a caller can partition or cheaply invalidate
  their own cache by rotating the namespace. Charset-validated.
- **Map request `Cache-Control: max-age=0` → bypass** (strictest reading,
  consistent with the existing `no-cache`/`no-store` bypass).
- **Reject `s-maxage` and request `max-age>0` freshness windows.** RFC 9111's
  age/freshness model presupposes stored-age tracking and a revalidation (304
  /ETag) protocol the broker does not implement; honoring them would be a silent
  lie. TTL is the honest lever and `x-broker-cache-ttl` exposes it per request.

### 4. Opt-in span content capture (`BROKER_TRACING_CAPTURE_CONTENT`, default off)

When tracing is enabled **and** this flag is explicitly set, the `inference` span
gains `gen_ai.input.messages` and `gen_ai.output.messages` as **truncated JSON
strings** (`BROKER_TRACING_CONTENT_MAX_CHARS`, default 2000), following the OTel
GenAI `spanOnly` content-capturing mode. **Off by default**, with a loud warning
in the docs that prompts/responses may contain PII. Disabled is byte-for-byte
ADR 0012 (no span carries message content) — the existing tracing test that
asserts this stays green.

## Alternatives considered

- **Lower semantic threshold (0.80–0.90), as LiteLLM/Redis default.** Rejected as
  a default: higher hit rate but a 3–15% wrong-answer rate on a multi-tenant
  gateway with no eval loop. Operators can opt down after measuring.
- **Load the embedding model on demand for every lookup.** Rejected as the
  default: it violates the lazy-load invariant (a chat request would load a model
  it never requested). Offered as an explicit opt-in
  (`BROKER_SEMANTIC_CACHE_REQUIRE_RESIDENT=false`) for operators who accept it.
- **Fuzzy-match more than the prompt text** (e.g. ignore sampling params).
  Rejected: different sampling/tools/`response_format` can legitimately change the
  answer; keeping them in the exact-match bucket key is what makes a semantic hit
  safe.
- **An ANN index / numpy / a vector DB.** Rejected: adds a mandatory dependency
  for a bounded per-bucket list where pure-Python brute force is adequate.
- **Store an assembled stream from a streaming request.** Rejected: partial
  /errored streams are unsafe to cache; non-stream population + stream replay
  gives the same hit benefit with no partial-write risk.
- **Honor `s-maxage` / request `max-age`.** Rejected — no age/revalidation model
  (see §3).
- **Content capture on by default, or via log events.** Rejected: content is
  PII-bearing (default off per the spec); span attributes are the simplest shape
  our minimal tracer supports and match `spanOnly`.

## Consequences

- The semantic cache trades a small, bounded, documented false-positive risk for
  paraphrase hit-rate; strict non-prompt scoping, a high default threshold, the
  resident-only gate, and deterministic-only default are the mitigations, each
  tested. It adds no mandatory dependency.
- The lazy-load invariant is preserved: by default a chat request never loads an
  embedding model; the only way to opt out is an explicit setting.
- Streaming callers now benefit from the cache; cache population semantics are
  unchanged (non-stream only), so there is no new partial-write risk.
- Per-request TTL/namespace give callers fine control without a revalidation
  protocol; rejected directives are documented so behavior is honest.
- Content capture stays off unless an operator opts in twice (tracing + capture);
  defaults reproduce v0.5.0 byte-for-byte.
- Every ADR 0012 deferral is now resolved (implemented or rejected); the roadmap
  is updated accordingly.

## Confirmation

Verified by the test suite (run from the worktree; semantic + content-capture
assertions use the in-process StateStore and an in-memory OTel exporter, and the
otel ones skip cleanly where the optional dep is absent — exercised green in a
private `[dev,otel]` venv):

- `tests/test_semantic_cache.py` — pure cosine + bucket-key unit tests;
  **semantic hit above threshold, miss below**; **miss when the embedding model
  is not resident (and no model load happens)**; **hit once resident**;
  `require_resident=false` allows on-demand embed; **scope isolation** across
  different keys, placements, and sampling/tools params (never a cross-scope
  semantic hit); `semantic-hit` + `x-broker-cache-similarity` headers;
  deterministic-only default; disabled = pass-through; bounded index eviction and
  TTL. **Follow-up:** `bucket_key` separates differing **system prompts** and
  **histories** (same last turn → different bucket) while a reworded last user turn
  shares a bucket; non-text content parts stay exact; `query_text` embeds only the
  last user turn; a stub embedder returning identical vectors proves differing
  system prompts / histories always MISS while a reworded last turn HITs; the
  **number of buckets** is LRU-bounded (`MAX_BUCKETS`).
- `tests/test_cache_streaming.py` — a non-streaming request populates the cache;
  a subsequent `stream: true` request for chat / completions / responses returns
  a **well-formed SSE replay** (`chat.completion.chunk` / `text_completion` /
  `response.*` events + `[DONE]`) with `x-broker-cache: hit`; a streaming miss
  never stores; semantic streaming replay.
- `tests/test_cache_controls.py` — `x-broker-cache-ttl` clamps the write TTL;
  **follow-up:** non-finite / overflowing TTLs (`nan`/`inf`/`-inf`/`1e400`/400
  digits) → `400`; `0` does not store (never forever); a huge finite override is
  clamped to the operator ceiling; `_clamp_ttl` only shortens. `x-broker-cache-namespace`
  isolates and invalidates; `Cache-Control: max-age=0` bypasses; a rejected
  `s-maxage` directive does not change behavior.
- `tests/test_cache_model_scope.py` — **follow-up (GAP 3):** a cascade escalation
  to a different model is **not stored** (`skipped_model_mismatch`, a second
  identical request still misses), while a clean cascade that stays on the primary
  stores and hits.
- `tests/test_cache_redis_ttl.py` — **follow-up:** against a fake Redis backend
  every write carries a **finite** TTL within the operator ceiling (huge override
  clamped), the default TTL is finite, and `x-broker-cache-ttl: 0` stores nothing
  (skipped unless the `[redis]` extra + `fakeredis` are present).
- `tests/test_tracing_content.py` — content capture **off by default** (no span
  carries messages); **on** when `BROKER_TRACING_CAPTURE_CONTENT=true` the
  `inference` span carries truncated `gen_ai.input.messages` /
  `gen_ai.output.messages`; truncation respected.
- Existing `tests/test_cache.py`, `tests/test_tracing.py`, and the v0.5.0 suite
  stay green unchanged, proving defaults reproduce v0.5.0. Gates: `ruff check` +
  `ruff format --check` + full `pytest` + `mypy`; `docs/openapi.yaml` unchanged
  (no route/schema change — only request/response headers).
