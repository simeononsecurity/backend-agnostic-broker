# Docker Compose Guide

Two Compose files:

- `docker-compose.yml` — base, **CPU-only**, hardened, CI-friendly.
- `docker-compose.gpu.yml` — GPU override (opt-in).

## Validate

```bash
make compose-config
# or:
docker compose -f docker-compose.yml config -q
docker compose -f docker-compose.yml -f docker-compose.gpu.yml config -q
```

## Base service

The `broker` service builds the image (with `VERSION`/`VCS_REF` args), publishes
`${BROKER_PORT:-8080}:8080`, mounts `registry.example.yaml` read-only at
`/config/registry.yaml` and a `models` volume read-only at `/models`, and sets
safe defaults (`BROKER_ENABLED_BACKENDS=["fake","ollama"]`, JSON logs,
`BROKER_GPU_VISIBLE=false`). Hardening: `read_only: true`, `tmpfs` for
`/tmp/broker` and `/var/cache/broker`, `cap_drop: [ALL]`,
`security_opt: [no-new-privileges:true]`, CPU/memory limits under `deploy`, and
a `/healthz` healthcheck.

## Secrets

API keys are **not** baked into the image. Provide `BROKER_API_KEYS` via env
injection (`BROKER_API_KEYS` in your shell/`.env`) or a Docker secret. Never
commit a filled `.env`.

```bash
export BROKER_API_KEYS='["my-key"]'
docker compose up -d --build
```

## GPU override

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
```

The override sets `BROKER_GPU_VISIBLE=true`, `BROKER_CUDA_VISIBLE_DEVICES`
(default `0`), enables all backends, reserves NVIDIA GPUs via
`deploy.resources.reservations.devices`, and raises `shm_size` for
tensor/pipeline parallelism. It requires the NVIDIA Container Toolkit on the
host. Because GPU passthrough only exists in the override, the base file stays
CPU/CI-safe.

## Lifecycle

```bash
make compose-up      # docker compose up -d --build
make compose-down    # docker compose down -v
```
