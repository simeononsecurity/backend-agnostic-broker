# How-to: the admin API and registry hot reload

Manage a running broker over HTTP without a restart: create/rotate/revoke
**virtual keys**, **unload/load** models on demand, and **hot-reload** the registry
transactionally. The admin plane is **default-off** and sits behind an
**admin-scoped** key — it adds no unauthenticated endpoints.

## When to use

- You manage keys centrally (CI, a control panel) instead of editing a file.
- You need to free a GPU (`unload`) or roll out a registry change with no downtime.

## Minimal config

```bash
BROKER_ADMIN_ENABLED=true
# authenticate with an admin-scope virtual key, or a flat admin key:
BROKER_ADMIN_API_KEYS=["YOUR_ADMIN_KEY"]
```

Use the header `X-Admin-Key: <admin-secret>` (or an admin-scope Bearer key).

## Key management API

```bash
ADMIN="YOUR_ADMIN_KEY"
# List (never returns secrets or hashes)
curl -s http://127.0.0.1:8080/admin/keys -H "X-Admin-Key: $ADMIN"
# Create (secret returned once)
curl -s -X POST http://127.0.0.1:8080/admin/keys -H "X-Admin-Key: $ADMIN" \
  -H 'content-type: application/json' \
  -d '{"label":"ci","tenant":"alpha","limits":{"rpm":10,"allowed_models":["chat"]}}'
# Rotate (secret returned once; old key grace-overlaps)
curl -s -X POST http://127.0.0.1:8080/admin/keys/brk_aaaa1111/rotate \
  -H "X-Admin-Key: $ADMIN" -H 'content-type: application/json' -d '{"grace_days":7}'
# Revoke
curl -s -X DELETE http://127.0.0.1:8080/admin/keys/brk_aaaa1111 -H "X-Admin-Key: $ADMIN"
```

## Runtime model control and hot reload

```bash
# Unload a model now (frees its GPU/process); next request lazy-reloads it.
curl -s -X POST http://127.0.0.1:8080/admin/models/llama3/unload -H "X-Admin-Key: $ADMIN"
# Hot-reload the registry: validated and applied transactionally (all-or-nothing).
curl -s -X POST http://127.0.0.1:8080/admin/registry/reload -H "X-Admin-Key: $ADMIN"
```

A reload that fails validation is **rejected whole** — the running registry is
never left half-applied. See the [API reference](../api-reference.md) and
[OpenAPI](../openapi.yaml) for exact request/response shapes and admin paths.

## Verify

```bash
curl -s http://127.0.0.1:8080/admin/keys -H "X-Admin-Key: YOUR_ADMIN_KEY" | python3 -m json.tool
# 200 with a keys list and no secret fields; 401/403 without a valid admin key.
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `404` on `/admin/*` | `BROKER_ADMIN_ENABLED=true` is required; otherwise the plane isn't mounted. |
| `401/403` | Use an admin-scope key or a `BROKER_ADMIN_API_KEYS` value via `X-Admin-Key`. |
| Reload rejected | That's intentional — fix the reported registry error; the live config is unchanged. |

## Example

[examples/governance](../../examples/governance/) covers the admin key API (tested
round-trip).

## Reference & decisions

- Reference: [API reference](../api-reference.md) · [configuration](../configuration.md)
- ADRs: [0012 caching, runtime control & observability](../adr/0012-caching-runtime-control-observability.md) ·
  [0016 governance v2](../adr/0016-governance-v2.md)
