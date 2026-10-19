# `/v1/rerank` example (ADR 0013)

A Cohere/Jina-compatible reranking endpoint. Give it a `query` and a list of
`documents`; it returns them ordered by `relevance_score` (descending), with the
original `index` preserved. Routed by model name through the broker's
pool/lazy-load/failover machinery (no `auto`), with per-key governance, placement,
usage logging, and response caching — exactly like `/v1/embeddings`.

See [`docs/api-reference.md`](../../docs/api-reference.md) and
[ADR 0013](../../docs/adr/0013-rerank-endpoint.md).

## Files

- [`registry.yaml`](registry.yaml) — a **fake** reranker (no GPU, runs anywhere)
  plus a commented **llama.cpp** reranker for production.
- [`rerank.sh`](rerank.sh) — curl example.
- [`rerank.py`](rerank.py) — stdlib-only Python example (no SDK).

## Try it locally (no GPU, no downloads)

```bash
BROKER_AUTH_ENABLED=false BROKER_ENABLED_BACKENDS=fake \
BROKER_REGISTRY_PATH=examples/rerank/registry.yaml \
python -m broker          # serves on http://127.0.0.1:8080

# in another shell:
bash examples/rerank/rerank.sh
# or:
python examples/rerank/rerank.py
```

The fake backend scores by deterministic lexical overlap, so the "capital of
France" query ranks the Paris document first — reproducibly.

## Request / response

```json
POST /v1/rerank
{
  "model": "rerank-fake",
  "query": "What is the capital of France?",
  "documents": ["The capital of France is Paris.", "Bananas are a fruit."],
  "top_n": 2,
  "return_documents": true
}
```

```json
{
  "id": "rerank-...",
  "object": "rerank",
  "model": "rerank-fake",
  "results": [
    {"index": 0, "relevance_score": 0.5, "document": {"text": "The capital of France is Paris."}},
    {"index": 1, "relevance_score": 0.0, "document": {"text": "Bananas are a fruit."}}
  ],
  "usage": {"total_tokens": 12}
}
```

- `documents` items may be strings or `{"text": ...}` objects.
- `top_n` caps the number of results; `return_documents` (default `false`)
  controls whether each result echoes its document text (as `{"text": ...}`).
- A model without `supports_rerank` → `400`; unknown model → `404`. **Ollama and
  Strata rerank return `400`** (no native endpoint / unverified). The documents
  count is bounded by `BROKER_RERANK_MAX_DOCUMENTS` (default 1000).

## Production: a real reranker

Run a reranker GGUF with `llama-server` and point the broker at it on loopback:

```bash
llama-server -m bge-reranker-v2-m3-q8_0.gguf --reranking --pooling rank \
  --host 127.0.0.1 --port 8082 -ngl 99

BROKER_ALLOW_LOOPBACK_BACKENDS=true \
BROKER_REGISTRY_PATH=examples/rerank/registry.yaml python -m broker
# then: RERANK_MODEL=bge-reranker bash examples/rerank/rerank.sh
```

(Uncomment the `bge-reranker` entry in `registry.yaml` first.) vLLM reranker
models work identically with `backend: vllm`.
