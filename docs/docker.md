# Docker Guide

The broker ships a hardened, multi-stage image (`Dockerfile`).

## Build

```bash
make docker-build
# or:
docker build --build-arg VERSION=0.1.0 --build-arg VCS_REF=$(git rev-parse --short HEAD) \
  -t backend-agnostic-broker:0.1.0 .
```

Build args become OCI labels: `org.opencontainers.image.version` (`VERSION`) and
`org.opencontainers.image.revision` (`VCS_REF`), plus title/description/source/
licenses. Inspect with `docker inspect backend-agnostic-broker:0.1.0`.

## Image design

- **Multi-stage**: a `builder` stage compiles wheels from `requirements.txt`; the
  `runtime` stage installs them `--no-index` from the local wheel dir.
- **Minimal runtime**: `python:3.11-slim`; `pip`/`setuptools`/`wheel` are
  uninstalled from the runtime layer to reduce the vulnerability surface.
- **Non-root**: a system user/group `broker` (uid/gid `10001`); `ENTRYPOINT
  ["python","-m","broker"]`.
- **Writable dirs**: `BROKER_CACHE_DIR=/var/cache/broker` and
  `BROKER_TMP_DIR=/tmp/broker` are created and owned by `broker`, so the root fs
  can be mounted read-only with these as tmpfs/volumes.
- **Healthcheck**: calls the broker's own `/healthz` via `python -c` (no shell).

## Run (hardened, fake backend)

```bash
docker run --rm -p 8080:8080 \
  -e BROKER_AUTH_ENABLED=true \
  -e 'BROKER_API_KEYS=["my-key"]' \
  -e 'BROKER_ENABLED_BACKENDS=["fake"]' \
  -e BROKER_REGISTRY_PATH=/config/registry.yaml \
  -v "$(pwd)/registry.example.yaml:/config/registry.yaml:ro" \
  --read-only --tmpfs /tmp/broker --tmpfs /var/cache/broker \
  --cap-drop ALL --security-opt no-new-privileges:true \
  backend-agnostic-broker:0.1.0
```

## Smoke test

`make docker-smoke` builds and runs `scripts/docker_smoke.sh`, which starts the
container with the fake backend and asserts: `/healthz` is `ok`, `/v1/models`
returns `401` without a key and the model with a key, and a chat completion
returns a `chat.completion` object. It runs the container with
`--read-only --cap-drop ALL --security-opt no-new-privileges`.

## CPU vs GPU

The base image and Compose are CPU-only and CI-friendly. GPU passthrough is
opt-in via `docker-compose.gpu.yml` (NVIDIA Container Toolkit required) — see
`docs/docker-compose.md` and `docs/gpu-placement.md`. The broker does not assume
host runtimes exist inside the container; broker-managed backends expect their
runtime binaries to be present and allow-listed.
