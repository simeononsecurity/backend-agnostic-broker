# Docker Guide

The broker ships a hardened, multi-stage image (`Dockerfile`).

## Build

```bash
make docker-build
# or:
docker build --build-arg VERSION=0.3.0 --build-arg VCS_REF=$(git rev-parse --short HEAD) \
  -t backend-agnostic-broker:0.3.0 .
```

Build args become OCI labels: `org.opencontainers.image.version` (`VERSION`) and
`org.opencontainers.image.revision` (`VCS_REF`), plus title/description/source/
licenses. Inspect with `docker inspect backend-agnostic-broker:0.3.0`.

## Image variants (lean vs bundled router LLM)

Two variants are published per release:

| Tag | Build | Contents | Size |
|-----|-------|----------|------|
| `:X.Y.Z`, `:latest` | default | broker only; `auto` uses the heuristic router | ~272 MB |
| `:X.Y.Z-llm`, `:latest-llm` | `--build-arg WITH_ROUTER_LLM=1` | + CPU `llama-cpp-python` + a small Q4 GGUF at `/models/router.gguf` | ~1.35 GB |

```bash
# Self-contained LLM router (CPU) baked in:
docker build --build-arg WITH_ROUTER_LLM=1 -t backend-agnostic-broker:llm .
# Override the bundled model if you want a different one:
docker build --build-arg WITH_ROUTER_LLM=1 \
  --build-arg ROUTER_MODEL_URL=https://.../your-model.Q4_K_M.gguf -t broker:llm .
```

The lean image's `auto` alias still works (zero-dependency heuristic). The `-llm`
image sets `BROKER_ROUTER_MODEL_PATH=/models/router.gguf` so `router_backend=auto`
uses the GGUF classifier; if the file is absent it falls back to the heuristic.
See [auto-router.md](auto-router.md).

## Pull published images (GHCR)

Each tagged release publishes both variants to GHCR:

```bash
docker pull ghcr.io/simeononsecurity/backend-agnostic-broker:latest       # lean
docker pull ghcr.io/simeononsecurity/backend-agnostic-broker:latest-llm   # bundled CPU classifier
# or pin a version: :0.3.0  /  :0.3.0-llm
```

> Packages are **private** by default — run `docker login ghcr.io` first, or set
> the package visibility to public on GitHub. See [ci.md](ci.md).

## Run the bundled-LLM image

The `-llm` image is self-contained: it ships the CPU `llama-cpp-python` runtime
(incl. `libgomp1`) and a Q4 GGUF, and points `BROKER_ROUTER_MODEL_PATH` at it, so
`router_backend=auto` uses the LLM classifier out of the box.

```bash
docker run --rm -p 8080:8080 \
  -e BROKER_AUTH_ENABLED=false \
  -e 'BROKER_ENABLED_BACKENDS=["fake"]' \
  -e BROKER_REGISTRY_PATH=/config/registry.yaml \
  -v "$(pwd)/registry.example.yaml:/config/registry.yaml:ro" \
  --tmpfs /tmp/broker --tmpfs /var/cache/broker \
  ghcr.io/simeononsecurity/backend-agnostic-broker:latest-llm
```

Confirm the classifier is actually in use (not the heuristic fallback):

```bash
curl -s localhost:8080/status \
  | python3 -c 'import sys,json;r=json.load(sys.stdin)["router"];print("backend:",r["backend"]);print("last via:",(r["recent_decisions"][-1]["via"] if r["recent_decisions"] else "-"))'
# want:  backend: llm   AND a decision with  via: llm
# (backend:llm alone can still fall back to heuristic if the model fails to load)
```

**Use a different classifier model** without rebuilding — mount a GGUF and point at
it (the runtime is only present in the `-llm` image; a mounted model on the lean
image stays on the heuristic because `llama-cpp-python` isn't installed there):

```bash
docker run ... \
  -v "$(pwd)/my-router.gguf:/models/router.gguf:ro" \
  -e BROKER_ROUTER_MODEL_PATH=/models/router.gguf \
  -e BROKER_ROUTER_BACKEND=llm \
  -e BROKER_ROUTER_THREADS=4 -e BROKER_ROUTER_MODEL_CTX=2048 \
  ghcr.io/simeononsecurity/backend-agnostic-broker:latest-llm
```

Model prioritization for `auto` (tags, `priority`, `cost_tier`, overrides) is
configured in the **registry**, not the image — see [auto-router.md](auto-router.md).

## Image design

- **Multi-stage**: a `pydeps` stage installs `requirements.txt` into a relocatable
  `/install` prefix; an optional `router` stage compiles `llama-cpp-python` and
  fetches the GGUF; the `runtime` stage copies only the installed packages (and,
  for the LLM variant, the model) — so no wheels layer or compiler ships in the
  final image.
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
  backend-agnostic-broker:0.3.0
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
