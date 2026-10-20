# How-to: pools and routing strategies

Serve one logical model name from **multiple backend hosts** (a pool) and choose
how requests spread across members: `sticky` (default), `least_busy`,
`round_robin`, or `cache_aware`.

## When to use which strategy

| Strategy | Use when | Trade-off |
|---|---|---|
| `sticky` (default) | A conversation must stay on one member (warm context, per-session pinning) | Uneven load if sessions are long-lived |
| `least_busy` | You want the best **tail latency** (p95 TTFT) and even load | Ignores cache locality |
| `round_robin` | Simple even spread, stateless | Ignores load and cache |
| `cache_aware` | Workloads share prompt **prefixes** (system prompts, RAG, long chats) and you want to save **prefill work** | Within ~10% of `least_busy` on p95; does **not** beat it on tail latency |

All figures are from the simulated queueing benchmark — see
[routing-and-health.md](../routing-and-health.md#benchmarks). Pick by your goal:
latency → `least_busy`; GPU prefill work / energy → `cache_aware`; contractual
per-session pinning → `sticky`.

## Minimal config

Give several registry entries the **same `pool` name**; clients call that name:

```yaml
# registry.yaml
models:
  - { alias: chat-1, backend: ollama, runtime_model_id: llama3.1:8b, pool: chat,
      base_url: ${OLLAMA_URL_1}, readiness: { kind: http, path: /api/tags, expect_status: 200 } }
  - { alias: chat-2, backend: ollama, runtime_model_id: llama3.1:8b, pool: chat,
      base_url: ${OLLAMA_URL_2}, readiness: { kind: http, path: /api/tags, expect_status: 200 } }
```

Clients then request `"model": "chat"`. Default routing is `sticky`; failover
between members is automatic.

## Full config

```bash
# Pick the strategy:
BROKER_ROUTE_STRATEGY=cache_aware          # sticky | least_busy | round_robin | cache_aware
# Sticky map bound (in-memory; see redis-multi-replica guide for shared state):
BROKER_ROUTE_STICKY_MAX_ENTRIES=10000
BROKER_ROUTE_STICKY_TTL_SECONDS=0          # 0 = no idle expiry
# cache_aware tuning (ADR 0010/0018):
BROKER_CACHE_AWARE_BETA=0.5                # load weight vs prefix reuse
BROKER_CACHE_AWARE_BLOCK_CHARS=128         # prefix block granularity
BROKER_CACHE_AWARE_MAX_ENTRIES=10000
BROKER_CACHE_AWARE_TTL_SECONDS=1800
```

Sticky session identity is taken from the `X-Session-Id` header, else the OpenAI
`user` field, else the client IP.

## Verify

```bash
# Sticky: the same session pins to one member.
curl -s -H 'X-Session-Id: alice' -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{"model":"chat","messages":[{"role":"user","content":"hi"}]}' \
  http://127.0.0.1:8080/v1/chat/completions
# Inspect routing + cache-aware stats:
curl -s -H "Authorization: Bearer YOUR_API_KEY" http://127.0.0.1:8080/status \
  | python3 -m json.tool
# cache_aware block shows enabled/lookups/hits/hit_rate; per-member `inflight` shows load.
```

Expected under `cache_aware`: repeated requests sharing a system prompt land on
the same member, and `/status.cache_aware.hit_rate` climbs.

## Troubleshooting

| Symptom | Fix |
|---|---|
| One member hot-spots under `cache_aware` | Raise `BROKER_CACHE_AWARE_BETA` (e.g. 0.5→1.0). |
| Low `hit_rate` | Lower `BROKER_CACHE_AWARE_BLOCK_CHARS` or raise TTL/`MAX_ENTRIES`; confirm your workload shares prefixes. |
| Sessions not pinning | Send a stable `X-Session-Id`; across replicas you need Redis (see below). |

## Examples

[examples/cache-aware-routing](../../examples/cache-aware-routing/) ·
[examples/docker-compose-redis](../../examples/docker-compose-redis/) (shared sticky state)

## Reference & decisions

- Reference: [routing and health](../routing-and-health.md) · [configuration](../configuration.md)
- ADRs: [0003 routing & health](../adr/0003-routing-and-health.md) ·
  [0010 cache-aware routing](../adr/0010-cache-aware-pool-routing.md) ·
  [0018 cache-aware v2](../adr/0018-cache-aware-v2.md)
