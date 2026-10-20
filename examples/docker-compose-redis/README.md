# Redis multi-replica

Run **two (or more) broker replicas** behind a load balancer, sharing one
**Redis** so sticky routing, rate-limit counters, member cooldowns, and cache
state are consistent no matter which replica handles a request (ADR 0009).

Without Redis each replica keeps this state in memory, so two replicas can pin
the same session to different backend members and count rate limits separately.
Point every replica at the same `BROKER_REDIS_URL` and the state becomes shared.

## Files

- `docker-compose.yml` — Redis + `broker-1` + `broker-2` + an nginx load balancer.
- `nginx.conf` — round-robins across the replicas, forwards `X-Session-Id`.
- `registry.example.yaml` — one pooled `chat` model over three fake members.
- `.env.example` — the Redis settings (placeholders only).

## Run it

```bash
cd examples/docker-compose-redis
docker compose up -d --scale broker-1=1 --scale broker-2=1
curl -s -H "Authorization: Bearer YOUR_API_KEY" \
  -H 'X-Session-Id: alice' \
  -d '{"model":"chat","messages":[{"role":"user","content":"hi"}]}' \
  http://127.0.0.1:8080/v1/chat/completions
```

Repeat with the same `X-Session-Id`: the session stays pinned to one member
across both replicas because the sticky map lives in Redis. Watch it in
`/status` under each model's routing state.

## Verify the shared state locally (no Docker)

With a Redis reachable at `redis://127.0.0.1:6379/0`, start two local brokers on
different ports with the same `BROKER_REDIS_URL` and confirm a session pins to
the same member from both. The bundled test does this in-process with
`fakeredis`, so it runs in CI with no real Redis.

## Settings that matter

| Setting | Purpose |
|---|---|
| `BROKER_REDIS_URL` | Enables shared state. Unset = in-memory (single replica). |
| `BROKER_REDIS_PREFIX` | Namespaces keys so multiple stacks can share one Redis. |
| `BROKER_REDIS_STICKY_MAX_ENTRIES` | Global LRU bound on the sticky map (ADR 0017). |
| `BROKER_ROUTE_STICKY_TTL_SECONDS` | Optional idle expiry for sticky pins. |

## See also

- Guide: [redis-multi-replica](../../docs/guides/redis-multi-replica.md)
- Reference: [routing and health](../../docs/routing-and-health.md) ·
  [configuration](../../docs/configuration.md)
- ADR: [0009 reliability & scale](../../docs/adr/0009-routing-reliability-and-scale.md)

Tested by `tests/test_examples.py` (registry + env load; shared sticky state via
`fakeredis`, skipped if the redis extra is absent).
