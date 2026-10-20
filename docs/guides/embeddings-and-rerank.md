# How-to: embeddings and rerank (a RAG walkthrough)

Use the broker for the two retrieval primitives alongside chat: **`/v1/embeddings`**
(vectors for similarity search) and **`/v1/rerank`** (cross-encoder reordering).
Together with chat they make a full RAG pipeline: **embed → retrieve → rerank →
chat**.

Both are addressed by **model name** directly — never via `auto` (there's nothing
to classify). Only models with `supports_rerank: true` may serve `/v1/rerank`;
everything else returns `400`. Ollama/Strata rerank is not supported.

## When to use

- **Embeddings**: semantic search, clustering, caching, retrieval recall.
- **Rerank**: tighten retrieval precision before passing context to an LLM — a
  cross-encoder reads query+document together, far more accurate than vector
  similarity alone.

## Minimal config

```yaml
# registry.yaml
models:
  - { alias: embed,  backend: ollama, runtime_model_id: nomic-embed-text,
      base_url: ${OLLAMA_BASE_URL}, readiness: { kind: http, path: /api/tags, expect_status: 200 },
      router_eligible: false }
  - { alias: rerank, backend: llamacpp, runtime_model_id: bge-reranker-v2-m3,
      model_path: /models/bge-reranker-v2-m3-q8_0.gguf, base_url: http://127.0.0.1:8082,
      supports_rerank: true, router_eligible: false }
```

For the reranker, run `llama-server` with `--reranking --pooling rank` (see the
annotated [`registry.example.yaml`](../../registry.example.yaml)).

## The RAG flow

```bash
# 1. Embed query + corpus (rank client-side by cosine for coarse recall):
curl -s -H "Authorization: Bearer YOUR_API_KEY" -H 'content-type: application/json' \
  -d '{"model":"embed","input":"What is the capital of France?"}' \
  http://127.0.0.1:8080/v1/embeddings

# 2. Rerank the retrieved candidates for precision:
curl -s -H "Authorization: Bearer YOUR_API_KEY" -H 'content-type: application/json' \
  -d '{"model":"rerank","query":"What is the capital of France?",
       "documents":["The capital of France is Paris.","Berlin is the capital of Germany."],
       "top_n":1,"return_documents":true}' \
  http://127.0.0.1:8080/v1/rerank

# 3. Chat with the top context grounding the answer (model: your chat model or "auto").
```

## Verify

Expected shapes: `/v1/embeddings` returns `{"object":"list","data":[{"embedding":[...]}]}`;
`/v1/rerank` returns `{"results":[{"index":0,"relevance_score":...,"document":{...}}]}`
sorted best-first, with the France/Paris document ranked above the Germany one.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `400` on `/v1/rerank` | The model needs `supports_rerank: true`; Ollama/Strata rerank is unsupported. |
| Empty/odd embeddings | Confirm the model is an embedding model and is enabled. |
| Rerank order looks wrong | Check you're sending the real reranker, not a chat model. |

## Examples

[examples/rag-pipeline](../../examples/rag-pipeline/) (full embed→rerank→chat,
tested) · [examples/rerank](../../examples/rerank/) (the endpoint alone).

## Reference & decisions

- Reference: [API reference](../api-reference.md) · [model registry](../model-registry.md)
- ADRs: [0006 OpenAI endpoint surface](../adr/0006-openai-endpoint-surface.md) ·
  [0013 rerank endpoint](../adr/0013-rerank-endpoint.md)
