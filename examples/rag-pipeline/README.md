# RAG pipeline: embed → retrieve → rerank → chat

A complete retrieval-augmented-generation flow through the broker, using three
models — an **embedding** model, a **reranker**, and a **chat** model — all on the
dependency-free `fake` backend so it runs with no GPU or downloads.

The pattern: embed the query and corpus, retrieve the top-k by vector similarity
(coarse recall), rerank those candidates with a cross-encoder (precision), then
feed the best context to the chat model (grounded generation).

```
query ──embed──▶ cosine top-k ──/v1/rerank──▶ top-n context ──/v1/chat──▶ answer
         │                                                        ▲
corpus ──┘                                                        │
         └──────────────── grounding context ────────────────────┘
```

## Files

- `registry.yaml` — fake `chat`, `embed`, and `rerank` models.
- `rag.py` — the full pipeline via the OpenAI SDK (+ a stdlib POST for `/v1/rerank`).
- `curl.sh` — the broker calls (`/v1/embeddings`, `/v1/rerank`, `/v1/chat/completions`).
- `.env.example` — copy to `.env`.

## Run it

```bash
set -a && . ./examples/rag-pipeline/.env.example && set +a
python -m broker
# another shell:
python examples/rag-pipeline/rag.py
```

Expected: the Paris documents rank first after reranking, and the chat answer
echoes the grounded context. (The fake reranker scores by lexical overlap, so the
France/Paris documents beat the Germany and banana ones deterministically.)

## Why a reranker on top of embeddings?

Vector retrieval is fast but approximate — it recalls *candidates*. A cross-encoder
reranker reads the query and each candidate **together**, so it reorders them far
more precisely before they reach the LLM. `/v1/rerank` is addressed by the model
name directly (never via `auto`); only models with `supports_rerank: true` may
serve it. See [ADR 0013](../../docs/adr/0013-rerank-endpoint.md).

## Swap in real models

| Role | Real example |
|---|---|
| embed | Ollama `nomic-embed-text`, or any OpenAI-compatible embedding endpoint |
| rerank | llama.cpp `bge-reranker-v2-m3` (`--reranking --pooling rank`) or a vLLM reranker |
| chat | Ollama `llama3.1:8b`, vLLM, or llama.cpp |

Keep the registry aliases (`embed` / `rerank` / `chat`) and only change each
model's `backend` / `runtime_model_id` / `base_url`.

## See also

- Guide: [embeddings and rerank](../../docs/guides/embeddings-and-rerank.md)
- Reference: [API reference](../../docs/api-reference.md)
- ADRs: [0013 rerank](../../docs/adr/0013-rerank-endpoint.md) ·
  [0006 OpenAI endpoint surface](../../docs/adr/0006-openai-endpoint-surface.md)

Tested by `tests/test_examples.py` (registry loads; the embed → rerank → chat flow
runs against the fake backend and the Paris document ranks first).
