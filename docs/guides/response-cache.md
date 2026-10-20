# How-to: the response cache (exact, semantic, streaming replay)

Cache completions so identical (or semantically similar) requests return instantly
without hitting a backend. Two layers, both **default-off** and scope-isolated per
key/tenant: an **exact** cache (hash of the normalized request) and a **semantic**
cache (embedding similarity). Streamed responses are cached and **replayed** as SSE.

Every response carries an `x-broker-cache` header: `miss`, `hit`, or `semantic-hit`.

## When to use

- Repeated identical prompts (health checks, retries, popular queries) → exact cache.
- Paraphrased but equivalent prompts → semantic cache.
- Keep it off when every request must reach the model (freshness-critical, or
  non-deterministic sampling you want preserved).

By default only **deterministic** requests are cached
(`BROKER_RESPONSE_CACHE_DETERMINISTIC_ONLY=true`), so `temperature>0` sampling is
not cached unless you opt in.

## Minimal config (exact)

```bash
BROKER_RESPONSE_CACHE_ENABLED=true
```

## Full config

```bash
# Exact cache
BROKER_RESPONSE_CACHE_ENABLED=true
BROKER_RESPONSE_CACHE_TTL_SECONDS=300
BROKER_RESPONSE_CACHE_MAX_ENTRIES=1000
BROKER_RESPONSE_CACHE_MAX_BYTES=1000000
BROKER_RESPONSE_CACHE_DETERMINISTIC_ONLY=true

# Semantic cache (ADR 0014) — needs an embedding model
BROKER_SEMANTIC_CACHE_ENABLED=true
BROKER_SEMANTIC_CACHE_MODEL=echo-embed       # a registry embedding model
BROKER_SEMANTIC_CACHE_THRESHOLD=0.95         # cosine similarity to count as a hit
BROKER_SEMANTIC_CACHE_TTL_SECONDS=300
BROKER_SEMANTIC_CACHE_MAX_ENTRIES=128
BROKER_SEMANTIC_CACHE_REQUIRE_RESIDENT=true  # only use the embed model if already loaded
```

Per-request overrides are supported via cache-control headers (see the
[caching example](../../examples/caching/) and [API reference](../api-reference.md)).

## Verify

```bash
BODY='{"model":"echo-chat","messages":[{"role":"user","content":"hi"}],"temperature":0}'
curl -sD- -o/dev/null -H "Authorization: Bearer YOUR_API_KEY" -H 'content-type: application/json' \
  -d "$BODY" http://127.0.0.1:8080/v1/chat/completions | grep -i x-broker-cache  # -> miss
curl -sD- -o/dev/null -H "Authorization: Bearer YOUR_API_KEY" -H 'content-type: application/json' \
  -d "$BODY" http://127.0.0.1:8080/v1/chat/completions | grep -i x-broker-cache  # -> hit
```

Expected: first request `x-broker-cache: miss`, second `hit`; a paraphrase with the
semantic cache on returns `semantic-hit`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Always `miss` | Request must be deterministic (`temperature:0`) unless you disable `DETERMINISTIC_ONLY`. |
| Semantic cache never hits | Ensure `BROKER_SEMANTIC_CACHE_MODEL` is a real embedding model; lower the threshold; set `REQUIRE_RESIDENT=false` to embed on demand. |
| Stale answers | Lower the TTL, or send a cache-control header to bypass. |
| Cross-tenant leakage | Doesn't happen — cache entries are scope-isolated per key/tenant. |

## Example

[examples/caching](../../examples/caching/) — exact and semantic caching, tested
against the fake backend.

## Reference & decisions

- Reference: [configuration](../configuration.md) · [API reference](../api-reference.md)
- ADRs: [0012 caching, runtime control & observability](../adr/0012-caching-runtime-control-observability.md) ·
  [0014 semantic cache & extensions](../adr/0014-semantic-cache-and-cache-extensions.md)
