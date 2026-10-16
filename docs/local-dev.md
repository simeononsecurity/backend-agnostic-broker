# Local Development Guide

## Prerequisites

- Python 3.11+ (the project `.venv`), Docker (for image/compose work).

## Setup

```bash
make venv          # python -m venv .venv
make install       # pip install -e ".[dev]"  (ruff, pytest, etc.)
```

## Configure

```bash
cp .env.example .env            # set BROKER_API_KEYS (or BROKER_AUTH_ENABLED=false for dev)
cp registry.example.yaml registry.yaml
```

For the simplest loop, use the fake backend only:

```bash
BROKER_API_KEYS='["my-key"]' BROKER_ENABLED_BACKENDS='["fake"]' make run
```

`make run` executes `python -m broker` (uvicorn) and expects `registry.yaml` and
the required env to be present. The broker starts with **no models loaded**; the
first chat request lazily loads its selected model.

## Everyday targets (Makefile)

| Target | Action |
|--------|--------|
| `make venv` | Create the virtualenv. |
| `make install` | Install runtime + dev deps (`-e ".[dev]"`). |
| `make lint` | `ruff check broker tests`. |
| `make fmt` | `ruff check --fix` + `ruff format`. |
| `make typecheck` | `mypy broker` (advisory). |
| `make test` | `pytest -q`. |
| `make cov` | Tests with coverage. |
| `make run` | Run the broker locally. |
| `make openapi` | Regenerate `docs/openapi.yaml`. |
| `make docker-build` | Build the image. |
| `make docker-smoke` | Build + container smoke test. |
| `make compose-config` | Validate Compose files. |
| `make clean` | Remove caches. |

## Try it

```bash
curl -s http://127.0.0.1:8080/healthz
curl -s -H "Authorization: Bearer my-key" http://127.0.0.1:8080/v1/models
curl -s -H "Authorization: Bearer my-key" -H 'content-type: application/json' \
  -d '{"model":"echo-fake","messages":[{"role":"user","content":"Hi"}]}' \
  http://127.0.0.1:8080/v1/chat/completions
```

See `docs/api-reference.md` and `docs/troubleshooting.md`.
