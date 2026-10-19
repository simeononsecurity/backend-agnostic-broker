# ADR 0013: `/v1/rerank` endpoint (Cohere/Jina-compatible)

- Status: Accepted
- Date: 2026-10-19
- Supersedes: none
- Related: extends [ADR 0006](0006-openai-endpoint-surface.md) (OpenAI endpoint
  surface); `broker/adapters/base.py`, `broker/adapters/httpbase.py`,
  `broker/adapters/fake.py`, `broker/manager.py`, `broker/app.py`,
  `broker/registry.py`, `broker/config.py`, `docs/api-reference.md`,
  `docs/model-registry.md`, research note
  [`docs/research/adr-0013-rerank.md`](../research/adr-0013-rerank.md)

## Context

ADR 0006 built the OpenAI surface and implemented `/v1/embeddings` as a new
adapter method (routed by model name, no `auto`, through the pool/lazy-load/
failover machinery), but **deferred `/v1/rerank`** (roadmap item 2c). Reranking
is the other half of a local RAG pipeline: a cross-encoder scores a `query`
against a set of candidate `documents` and returns them ordered by relevance.
Clients (LangChain, LlamaIndex, OpenWebUI's `ExternalReranker`, Haystack) target
the **Cohere/Jina rerank wire shape**, and the two local runtimes we manage —
vLLM and `llama.cpp`'s `llama-server` — already expose a Cohere/Jina-compatible
`/v1/rerank` natively (research note §3–§4). Ollama has **no** native rerank
endpoint; the only ways to fake one on Ollama (embedding-magnitude or yes/no
logprobs) are self-described hacks that read the wrong model head (research §6).

The broker must add rerank without violating its invariants: lazy per-model
loading, no silent cross-model fallback, fail-closed placement/scoping, and
backends that cannot do a thing returning a clear `400` rather than a wrong
answer.

## Decision

We will add `POST /v1/rerank` as a **new adapter method** (pattern B from
`references/openai-endpoints.md`), mirroring `/v1/embeddings`:

- **Wire contract (Cohere/Jina-compatible).** Request: `model`, `query`
  (non-empty string), `documents` (non-empty list of strings or `{"text": …}`
  objects), optional `top_n`, optional `return_documents` (default `false`), and
  justified passthroughs (`max_tokens_per_doc`/`max_chunks_per_doc`). Response:
  `{id, model, object:"rerank", results:[{index, relevance_score, document?}],
  usage}` — `index` is the **original** document ordinal, results are **sorted by
  `relevance_score` descending**, and `top_n` caps the count. The broker is the
  normalization authority: it re-sorts, applies `top_n`, and attaches
  `document` (`{"text": …}` from the *request* documents) only when
  `return_documents` is true (omitted, never `null`, otherwise).
- **Adapter contract.** A non-abstract `rerank()` on `BackendAdapter` defaults to
  `400` (`does not support rerank`). `HTTPBackendAdapter.rerank()` proxies to the
  runtime's `/v1/rerank` (configurable `rerank_path`), pinning `runtime_model_id`
  and normalizing field names (`score`→`relevance_score`, `data`→`results`) — this
  serves **vLLM** and **llama.cpp**. `FakeAdapter.rerank()` computes a
  **deterministic Jaccard lexical-overlap** score so CI asserts ordering with no
  live model. **Ollama** and **Strata** override `rerank()` to raise a clear
  `400` (Ollama has no native endpoint and we refuse the embeddings-cosine
  substitute; Strata rerank is unverified).
- **Manager.** `rerank()` / `_serve_rerank()` mirror `embeddings()` /
  `_serve_embeddings()`: pool selection, lazy load, concurrency gating,
  retries/cooldowns, failover, placement + per-key scoping — **no `auto`**
  (nothing to classify). A fail-closed capability gate rejects a model/pool whose
  members are not all `supports_rerank` with `400` before any load.
- **Registry.** `supports_rerank: bool = False` on `ModelEntry`, surfaced in
  `/v1/models` capabilities (pool = AND across members; `auto` descriptor = OR).
- **Config.** `BROKER_RERANK_MAX_DOCUMENTS` (default `1000`, matching Cohere's
  "≤1,000 recommended") caps the documents count; body size is still bounded by
  `BROKER_MAX_REQUEST_BYTES`.
- **Governance / usage / placement / cache.** The endpoint runs the full ADR
  0008 admission + reconcile, logs an ADR 0007 usage row (`endpoint="rerank"`),
  honors ADR 0011 placement, and is **response-cache eligible as
  always-deterministic** (ADR 0012) — a rerank over the same `(query, documents,
  top_n, return_documents)` is reproducible, exactly like embeddings.

## Alternatives considered

- **Emulate rerank on Ollama via embeddings cosine / magnitude.** Rejected: it is
  a silent quality substitute (reads the embedding layer, not the cross-encoder
  classification head — research §6). We return a clear `400` instead, honoring
  the no-silent-fallback invariant.
- **Translate rerank to the chat pipeline (shim, pattern A).** Rejected: a
  reranker is a different model class chat cannot generate; it needs a real
  adapter method, like embeddings.
- **Pass the backend response straight through.** Rejected: TEI uses a different
  shape (`{texts}`→`{index, score}`) and backends differ on `top_n`/document
  echo. The broker normalizes so every backend yields one consistent Cohere/Jina
  response.
- **Support TEI's native `/rerank` as a backend.** Rejected for this ADR: TEI is
  not a registered broker backend and its wire shape is non-standard; a TEI
  deployment exposing a Cohere-compatible path can be wired as an `llamacpp`/http
  entry.
- **Route rerank through `auto`.** Rejected: there is nothing to classify; rerank
  is addressed by model name only (same as embeddings).

## Consequences

- **Positive.** Local RAG clients get a drop-in Cohere/Jina rerank against vLLM
  and llama.cpp reranker models, with the broker's pooling, lazy load, failover,
  governance, placement, usage, and caching for free. The fake backend makes the
  endpoint fully CI-testable.
- **Negative.** Ollama/Strata rerank is unavailable by design (clear `400`). The
  broker recomputes ordering/`top_n` and the `document` echo, so a backend's own
  `document` text is ignored (consistent, but not byte-identical to the backend).
- **Risks.** A reranker model mis-flagged `supports_rerank: true` on a
  non-rerank runtime would reach the backend and fail there; the adapter-level
  `400` on Ollama/Strata and the backend's own error bound the blast radius.
  Defaults reproduce v0.5.0 exactly (new setting default-safe; no model loads at
  startup; new capability defaults false).

## Confirmation

`tests/test_rerank.py` covers: input validation (missing/empty query, missing/
empty documents, documents-count cap, bad `top_n`), ordering (obvious query
ranks the relevant doc first), `top_n`, `return_documents` on/off, non-rerank
model ⇒ `400`, unknown model ⇒ `404`, auth ⇒ `401`, per-key `allowed_models`
scoping, placement applied, pool failover, usage recorded, response-cache hit,
and the Ollama/Strata adapter `400` guard. `tests/test_openai_endpoints.py` /
`docs/openapi.yaml` list the `/v1/rerank` path.

**Live local verification (no remote servers).** `/v1/rerank` was driven through
the broker against a real `llama.cpp` reranker (`bge-reranker-v2-m3` GGUF) run
locally in Docker with `--reranking --pooling rank` on `127.0.0.1`, with
`BROKER_ALLOW_LOOPBACK_BACKENDS=true`. The exact commands, the registry entry,
the request, the observed (correct) ordering, and the teardown (container +
weights removed) are recorded in `docs/testing.md` and the research note.

### Follow-up: strict upstream-result validation (no cache poisoning)

Orchestrator verification found that a *malformed* upstream rerank body was
either passed through (`200`) or crashed the request (`500`). The normalization
path is now the **total validation authority**: `build_rerank_response` requires
every result to be an object carrying a non-bool `int` `index` in
`[0, len(documents))` that is unique across the response, and a finite real
`relevance_score`; any violation raises `RerankContractError`, which
`HTTPBackendAdapter.rerank` maps to `BackendUnavailableError` (`502`). Results
are never partially emitted or silently coerced, the malformed body is never
written to the response cache (errors propagate uncached), and the failure
participates in pool failover/cooldown like any other backend error. A
non-JSON upstream body is caught by a shared `_json_or_unavailable` helper now
applied to the rerank, chat, and (non-stream) embeddings `resp.json()` call
sites — closing the same latent `500` on those paths. The upstream `id` is no
longer echoed: the response `id` stays broker-generated (`rerank-<request_id>`).

`tests/test_rerank_upstream.py` drives the real vLLM adapter/app against a tiny
in-process loopback upstream: one parametrized `502` case per contract violation
(out-of-range / negative / duplicate / float / bool index; string score; string
index; `NaN`/`Infinity` score; non-JSON body; results-not-a-list; non-object
result), a well-formed case asserting the broker-generated `id`, a fewer-results-
than-documents (top_n) positive case, chat/embeddings non-JSON ⇒ `502`, the
cache-not-poisoned assertion, and pool failover past a member returning a
malformed body.
