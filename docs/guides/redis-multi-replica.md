# How-to: Redis for multi-replica deployments

Run **multiple broker replicas** behind a load balancer and keep their routing and
governance state consistent by pointing them all at one **Redis**. Without Redis,
each replica holds sticky pins, rate-limit counters, member cooldowns, and cache
state **in memory**, so replicas disagree.

## When to use

- You run two or more broker replicas (horizontal scale or HA).
- You need rate limits and budgets enforced **across** replicas, not per-replica.
- You want a session pinned to the same backend member regardless of which replica
  handles the HTTP request.

Single-replica deployments don't need Redis — in-memory state is correct and
faster.

## Minimal config

```bash
BROKER_REDIS_URL=redis://<REDIS_HOST>:6379/0
```

Set the **same** URL on every replica. Requires the `redis` extra
(`pip install -e '.[redis]'`); the Docker image includes it.

## Full config

```bash
BROKER_REDIS_URL=redis://<REDIS_HOST>:6379/0
BROKER_REDIS_PREFIX=broker                 # namespace keys (share one Redis across stacks)
BROKER_REDIS_STICKY_MAX_ENTRIES=100000     # global LRU bound on the sticky map (ADR 0017)
BROKER_ROUTE_STICKY_TTL_SECONDS=0          # optional idle expiry for sticky pins
```

What becomes shared when Redis is set: sticky routing map, rate-limit / budget
counters, member cooldown records, the cache-aware prefix index, and (if enabled)
response-cache entries.

## Verify

```bash
# From two replicas (ports 8080 and 8081) with the same BROKER_REDIS_URL:
for p in 8080 8081; do
  curl -s -H 'X-Session-Id: alice' -H "Authorization: Bearer YOUR_API_KEY" \
    -d '{"model":"chat","messages":[{"role":"user","content":"hi"}]}' \
    http://127.0.0.1:$p/v1/chat/completions >/dev/null
  curl -s -H "Authorization: Bearer YOUR_API_KEY" http://127.0.0.1:$p/status \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["route_strategy"])'
done
```

Expected: session `alice` pins to the **same** member from both replicas, and a
per-key rate limit trips after the combined request count across replicas (not per
replica).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `tracing/redis is enabled but not installed` style error | Install the extra: `pip install -e '.[redis]'`. |
| Replicas still disagree | Confirm identical `BROKER_REDIS_URL` **and** `BROKER_REDIS_PREFIX` on every replica. |
| Redis memory growth | Lower `BROKER_REDIS_STICKY_MAX_ENTRIES` / set `BROKER_ROUTE_STICKY_TTL_SECONDS`. |

## Example

[examples/docker-compose-redis](../../examples/docker-compose-redis/) — two broker
replicas + Redis + an nginx load balancer.

## Reference & decisions

- Reference: [routing and health](../routing-and-health.md) · [configuration](../configuration.md)
- ADRs: [0009 reliability & scale](../adr/0009-routing-reliability-and-scale.md) ·
  [0017 reliability v2](../adr/0017-reliability-v2.md)
