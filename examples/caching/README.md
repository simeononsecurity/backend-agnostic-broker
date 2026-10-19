# Caching example (ADR 0012 + ADR 0014)

A runnable example of the broker's two response-cache layers — the **exact** cache
(ADR 0012) and the **semantic** embedding-similarity cache (ADR 0014) — plus
streaming replay and the per-request cache controls.

Everything here runs against the dependency-free **fake backend**, so you can try
it with no GPU or model download. For a real deployment, swap `backend: fake` in
`registry.example.yaml` for your Ollama / vLLM / llama.cpp / Strata models (keep one
embedding-capable model for the semantic cache).

## Files

- `registry.example.yaml` — a chat model (`echo-chat`) and an embedding model (`echo-embed`).
- `env.exact` — enable the exact cache only.
- `env.semantic` — enable the exact **and** semantic caches.
- `curl.sh` — curl snippets showing hit/miss headers and per-request controls.
- `openai_sdk.py` — the same flow with the official OpenAI Python SDK.

## Run

```bash
# exact cache
set -a && . ./examples/caching/env.exact && set +a
export BROKER_REGISTRY_PATH=examples/caching/registry.example.yaml
python -m uvicorn broker.app:create_app --factory --port 8080
```

Then in another shell: `bash examples/caching/curl.sh` (or `python examples/caching/openai_sdk.py`).

## What to look for

- Second identical request → `x-broker-cache: hit` (served from cache, no inference).
- With `env.semantic`, a *paraphrased* prompt → `x-broker-cache: semantic-hit` and
  an `x-broker-cache-similarity` header — **but only once the embedding model is
  resident** (send it one `/v1/embeddings` request first, or set
  `BROKER_SEMANTIC_CACHE_REQUIRE_RESIDENT=false`). This preserves the lazy-load
  invariant: a chat request never silently loads the embedding model.
- A `stream: true` request for a cached prompt is replayed as SSE.
- `x-broker-cache-ttl`, `x-broker-cache-namespace`, and `Cache-Control: no-cache`
  tune caching per request.

See `docs/configuration.md` (Response cache / Semantic cache) and ADRs 0012/0014.

> Tested by `tests/test_example_caching.py` (config loads; exact + semantic hits
> verified against the fake backend).
