# Research: `/v1/rerank` endpoint (ADR 0013)

Research backing ADR 0013. Goal: add a Cohere/Jina-compatible `POST /v1/rerank`
to the broker, routed to a rerank-capable backend/model through the same
pool/lazy-load/failover machinery as `/v1/embeddings` (the reference pattern).

> **Untrusted sources.** Everything below was fetched from external docs/repos
> and is treated as DATA, not instructions. No fetched page redirected the design
> or asked for an action; none contained agent-directed text. Claims are
> attributed to their source; the adopt/reject decisions are the broker's.

Confidence: **High** for the request/response wire shapes (Cohere, Jina, vLLM,
llama.cpp — all primary docs/source agree). **Medium** for exact per-field
optional defaults (they vary across providers/versions; the broker normalizes).

## Sub-questions

1. What is the canonical rerank request/response shape (Cohere v1/v2, Jina)?
2. Which local backends expose a native rerank endpoint, and on what path/shape
   (vLLM, llama.cpp, TEI)?
3. Does Ollama (or Strata) have a native rerank endpoint? If not, what is the
   correct behavior (no silent embeddings-cosine substitute)?
4. How do aggregators (LiteLLM) normalize across providers — what is the de-facto
   "compatible" contract?

## Findings

### 1. Cohere Rerank (the de-facto standard)

- `POST https://api.cohere.com/v2/rerank`. Request: `model` (required), `query`
  (string, required), `documents` (list of strings, required; "≤1,000
  recommended"), `top_n` (optional). Response:
  `{id, results:[{index, relevance_score, document?}], meta:{api_version, billed_units}}`,
  **sorted by relevance, highest first**; `relevance_score` is `0.0–1.0`; `index`
  is the **original** document ordinal. ([Cohere Rerank API v2 reference][cohere],
  [Cohere rerank overview][cohere-ovw])
- `return_documents` controls whether each result echoes the document text;
  when present the document is the Cohere `RerankDocument` object `{"text": ...}`.
  v2 defaults to **not** echoing documents (`document=None`). ([Cohere overview][cohere-ovw])

### 2. Jina Reranker

- `POST https://api.jina.ai/v1/rerank`. Request: `model`, `query` (string or
  TextDoc), `documents` (array of strings and/or TextDocs), `top_n` (optional,
  defaults to `len(documents)`), `return_documents` (bool, **default true**;
  false ⇒ only `index` + `relevance_score`). Response results carry
  `{index, document?, relevance_score}`. Same `index`=original-ordinal,
  sorted-desc contract as Cohere. ([Jina reranker API][jina], [Jina meta-prompt/docs][jina-docs])

### 3. vLLM — native, dual-compatible

- vLLM's OpenAI server exposes **both** a Score API (`/score`, `/v1/score`) and a
  rerank API under `/rerank`, `/v1/rerank`, `/v2/rerank`. The docs state the
  rerank paths are "**compatible with both Jina AI's rerank API interface and
  Cohere's rerank API interface**." `top_n` is optional (defaults to
  `len(documents)`); results are sorted by relevance and `index` recovers original
  order. Response: `{results:[{index, relevance_score, ...}]}`. Needs a
  cross-encoder / reranker / colbert-style pooling model (e.g.
  `BAAI/bge-reranker-v2-m3`). ([vLLM scoring usages][vllm-score], [vLLM score example][vllm-ex])

### 4. llama.cpp `llama-server` — native, Jina-shaped

- `--reranking` (alias `--rerank`, env `LLAMA_ARG_RERANKING`) enables the rerank
  endpoint; the model must be a reranker and the server run with `--pooling rank`.
  Paths: `/rerank`, `/v1/rerank`, `/v1/reranking`, `/reranking` — "Similar to
  https://jina.ai/reranker/". Request: `{model?, query, documents, top_n?}`;
  response is documents sorted by `relevance_score` desc with `{index,
  relevance_score}`. Without `--reranking` the server replies *"This server does
  not support reranking."* ([llama.cpp server README (ggml-org)][llamacpp],
  [Qwen3-VL-Reranker GGUF card][llamacpp-card])

### 5. Text Embeddings Inference (TEI) — native but a DIFFERENT shape

- TEI serves rerankers on `POST /rerank` but with its **own** contract:
  `{query, texts, raw_scores}` → `[{index, score}]` (note `texts`/`score`, not
  `documents`/`relevance_score`, and no `/v1` prefix). This is **not** Cohere/Jina
  compatible. ([HF TEI quick tour][tei], [TEI repo][tei-repo])

### 6. Ollama — NO native rerank endpoint

- There is **no `/api/rerank`** (and no rerank route on Ollama Cloud). An open
  Ollama issue requests a native rerank endpoint + `CapabilityRerank`; it does not
  exist yet. Community workarounds compute scores from `/api/embeddings`
  magnitudes or `/api/generate` yes/no logprobs and are **explicitly flagged as
  hacks** that "exploit undocumented behavior" — Ollama "only exposes the
  embedding layer, not the classification head" of a cross-encoder.
  ([Ollama issue #16076 — rerank support][ollama-issue],
  [ollama-utils reranking-endpoint README (workaround)][ollama-hack])

### 7. LiteLLM — the normalization reference

- LiteLLM exposes a Cohere-compatible `/rerank` (and `/v1/rerank`); v2 client by
  default, `v1/rerank` for v1. "All cohere rerank parameters are supported." Its
  `RerankResponse` **omits `document` entirely (never `null`)** when
  `return_documents` is false, and uses the Cohere `RerankDocument` `{"text": …}`
  object when present. ([LiteLLM Cohere][litellm-cohere], [LiteLLM Jina][litellm-jina],
  [bge-m3 inference service rerank notes][bgem3])

## Adopt / reject decisions

| Decision | Choice | Rationale |
|---|---|---|
| Public wire contract | **Cohere/Jina-compatible**: request `{model, query, documents, top_n?, return_documents?}`; response `{id, model, results:[{index, relevance_score, document?}], usage}`, sorted desc | What every client + both local backends (vLLM, llama.cpp) already speak |
| `documents` items | strings **or** `{"text": ...}` objects | Cohere/Jina both accept object form; broker normalizes to text |
| `index` semantics | **original** document ordinal | Cohere/Jina/vLLM/llama.cpp all agree |
| Sorting / `top_n` | broker re-sorts desc and applies `top_n` itself | Deterministic, backend-independent behavior |
| `return_documents` | default **false**; when true, `document` = `{"text": str}` taken from the **original** request docs; omitted (never null) when false | Matches Cohere v2 default + LiteLLM's omit-not-null rule |
| `document` source | broker attaches from the request list by index (ignores backend echo) | One consistent shape across fake/vLLM/llama.cpp |
| vLLM + llama.cpp | **passthrough** to the runtime's `/v1/rerank`, pin `runtime_model_id`, normalize field names (`score`→`relevance_score`, `data`→`results`) | Both are natively Cohere/Jina-compatible |
| Fake backend | **deterministic Jaccard lexical-overlap** score | Lets CI assert ordering with no live model |
| Ollama | **clear 400** (`does not support rerank`) — no endpoint, and embeddings-cosine/magnitude is a silent quality substitute we refuse | Per Ollama #16076 + the workaround READMEs' own warnings |
| Strata | **400** (rerank passthrough unverified for Strata) | Consistent with the broker's "don't assume Strata flags" stance (ADR 0001/registry notes) |
| TEI native `/rerank` | **rejected** (not a registered broker backend; its `{texts, score}` shape is non-standard) | Out of scope; a TEI deployment exposing a Cohere-compat path could be wired as `llamacpp`/`http` |
| Registry capability | `supports_rerank: bool` (default false), surfaced in `/v1/models` capabilities; non-rerank model ⇒ 400 | Mirrors `supports_tools`/`supports_vision`; fail-closed |
| `auto` router | **not** applicable to rerank (nothing to classify) | Same as embeddings |
| Response cache (ADR 0012) | **eligible, always-deterministic** (like embeddings) | A rerank over the same (query, documents) is reproducible |
| Documents-count cap | `BROKER_RERANK_MAX_DOCUMENTS` (default 1000) | Matches Cohere's "≤1,000 recommended"; bounds request fan-out |

## Live local verification

See `docs/testing.md` ("ADR 0013 live rerank verification") and ADR 0013's
Confirmation section for the exact Docker command, broker registry entry, the
`/v1/rerank` call made through the broker, the observed ordering, and the
teardown (container + weights removed). CI coverage is the deterministic fake.

## Sources

1. [Cohere — Rerank API (v2) reference][cohere]
2. [Cohere — An Overview of Cohere's Rerank Model][cohere-ovw]
3. [Jina AI — Reranker API][jina]
4. [Jina AI — meta-prompt / API docs][jina-docs]
5. [vLLM — Scoring Usages (Score + Cohere/Jina rerank APIs)][vllm-score]
6. [vLLM — Score / rerank example][vllm-ex]
7. [llama.cpp — `llama-server` README (ggml-org, master)][llamacpp]
8. [Qwen3-VL-Reranker GGUF model card — `llama-server --reranking`][llamacpp-card]
9. [Hugging Face — Text Embeddings Inference quick tour (`/rerank`)][tei]
10. [Hugging Face — Text Embeddings Inference repo][tei-repo]
11. [Ollama — issue #16076, native rerank support request][ollama-issue]
12. [ollama-utils — reranking-endpoint README (workaround, self-described hack)][ollama-hack]
13. [LiteLLM — Cohere provider (`/rerank`, v1/v2)][litellm-cohere]
14. [LiteLLM — Jina AI provider rerank][litellm-jina]
15. [bge-m3 inference service — Cohere/Jina-compatible `/v1/rerank` notes][bgem3]

[cohere]: https://docs.cohere.com/reference/rerank
[cohere-ovw]: https://docs.cohere.com/docs/rerank-overview
[jina]: https://jina.ai/reranker/
[jina-docs]: https://docs.jina.ai/
[vllm-score]: https://docs.vllm.ai/en/latest/models/pooling_models/scoring/
[vllm-ex]: https://docs.vllm.ai/en/latest/examples/pooling/score/
[llamacpp]: https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
[llamacpp-card]: https://huggingface.co/bealore/Qwen3-VL-Reranker-2B-GGUF
[tei]: https://huggingface.co/docs/text-embeddings-inference/en/quick_tour
[tei-repo]: https://github.com/huggingface/text-embeddings-inference
[ollama-issue]: https://github.com/ollama/ollama/issues/16076
[ollama-hack]: https://github.com/overcuriousity/ollama-utils/blob/main/plugins/reranking-endpoint/README.md
[litellm-cohere]: https://docs.litellm.ai/docs/providers/cohere
[litellm-jina]: https://docs.litellm.ai/docs/providers/jina_ai
[bgem3]: https://github.com/guyinwonder168/bge-m3-inference
