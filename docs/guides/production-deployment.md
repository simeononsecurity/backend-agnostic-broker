# How-to: production deployment

Run the broker safely in production: the hardened Docker image, a reverse proxy
with TLS, non-root execution, and a security checklist. The broker is built
security-first — this guide is about wiring it up without giving that away.

## When to use

You're past local trials and want a durable, exposed deployment.

## Deploy with Docker Compose

Start from the shipped [`docker-compose.yml`](../../docker-compose.yml): it runs
the image **non-root** with a **read-only root filesystem**, `no-new-privileges`,
all capabilities dropped, writable `tmpfs` for cache/tmp, resource limits, and a
non-loading healthcheck. API keys come from a Docker **secret**, never baked into
the image.

```bash
docker compose up -d
docker compose logs -f broker
```

GPU passthrough is opt-in via the override file
[`docker-compose.gpu.yml`](../../docker-compose.gpu.yml) — see
[docker-compose.md](../docker-compose.md). Published images:
`ghcr.io/simeononsecurity/backend-agnostic-broker:latest` (lean) and `:latest-llm`
(bundled CPU router classifier).

## Reverse proxy and TLS

Terminate TLS at a reverse proxy (nginx/Caddy/Traefik) in front of the broker; keep
the broker on an internal network. Minimal nginx:

```nginx
server {
    listen 443 ssl;
    server_name broker.example.com;
    ssl_certificate     /etc/ssl/broker.crt;
    ssl_certificate_key /etc/ssl/broker.key;
    location / {
        proxy_pass http://broker:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Session-Id $http_x_session_id;   # preserve sticky routing
        proxy_read_timeout 300s;                            # allow long streams
        proxy_buffering off;                                # stream SSE promptly
    }
}
```

For multiple replicas, put the proxy in front of them all and share state via Redis
— see [redis-multi-replica](redis-multi-replica.md). Caddy/Traefik get you automatic
TLS; the only broker-specific bits are forwarding `X-Session-Id` and disabling
response buffering for SSE.

## Non-root

The image already runs as a non-root user with a read-only root fs. If you run from
source under systemd, create a dedicated service user, point `BROKER_CACHE_DIR` /
`BROKER_TMP_DIR` at writable paths it owns, and keep `BROKER_ALLOWED_MODEL_ROOTS`
pinned to your model directory.

## Security checklist

- [ ] **Auth on**: `BROKER_AUTH_ENABLED=true`; use hashed virtual keys, not flat
      keys, for anything multi-tenant (see [virtual-keys](virtual-keys.md)).
- [ ] **TLS at the proxy**; the broker listens only on the internal network.
- [ ] **Secrets out of the image/registry**: keys via Docker secret or a gitignored
      `.env`; never commit real hosts/IPs/keys.
- [ ] **SSRF allow-list** stays tight: registry `base_url`s are auto-trusted; add
      others only via `BROKER_EXTRA_ALLOWED_BACKEND_URLS`.
- [ ] **Model roots pinned**: `BROKER_ALLOWED_MODEL_ROOTS` limits where process
      backends may load from.
- [ ] **Admin plane**: enable only if needed; put it behind an admin-scope key.
- [ ] **Request size cap**: `BROKER_MAX_REQUEST_BYTES` (default 2 MB).
- [ ] **Tracing content** stays off unless you accept logging prompts.
- [ ] **Resource limits** set (CPU/memory) and a **non-loading healthcheck** wired.

## Verify

```bash
curl -s https://broker.example.com/healthz          # {"status":"ok"} over TLS
curl -s -o /dev/null -w '%{http_code}\n' https://broker.example.com/v1/models   # 401 without a key
```

## Release gate & canary — lifecycle failure drills (ADR 0027)

Before a version ships or re-enters a production path, the **lifecycle failure
drills** (`tests/drills/`, ADR 0027) must be green:

- **Linux CI** — the `drills.yml` job runs `scripts/failure_drills.py --repeat 3`
  on every PR that touches `broker/**`, the drills, or the runner.
- **Local macOS** — run `python scripts/failure_drills.py --repeat 3` on a macOS
  host too (the POSIX teardown paths differ from Linux). **Attach the run log to
  the release evidence** for that version.
- **Canary** — a production canary must include at least a **stop-under-load**
  drill (D4/D5: SIGTERM during a cold load / with an open stream) and a
  **forced-kill** drill (D10: `SIGKILL` the broker, then `python -m broker
  cleanup`) before the broker re-enters a production path.

A flaky drill is a defect, not a retry — any failed repetition blocks the release.
See [testing.md](../testing.md#lifecycle-failure-drills-adr-0027).

## Troubleshooting

| Symptom | Fix |
|---|---|
| Streaming responses buffer | Disable proxy buffering (`proxy_buffering off`) and raise read timeout. |
| Sticky routing breaks behind the proxy | Forward `X-Session-Id` (or the `user` field). |
| Container can't write cache/tmp | Mount writable `tmpfs` at `BROKER_CACHE_DIR`/`BROKER_TMP_DIR` (the compose file does). |

## Reference & decisions

- Reference: [docker](../docker.md) · [docker-compose](../docker-compose.md) ·
  [security model](../security-model.md) · [threat model](../threat-model.md) ·
  [operations runbook](../operations-runbook.md)
- ADRs: [0005 image variants & publishing](../adr/0005-image-variants-and-publishing.md)
