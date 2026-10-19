# Contributing

Thanks for contributing to `backend-agnostic-broker`. This project is
security-sensitive; the rules below are not optional.

> **AI agents:** read [`AGENTS.md`](../AGENTS.md) first — it defines the
> mandatory change contract (update docs/ADRs/OpenAPI/CHANGELOG/tests with every
> code change), the quality gates, and where everything lives.

## Setup

```bash
make venv && make install     # Python 3.11+, installs -e ".[dev]"
source .venv/bin/activate
```

## Before you open a PR

```bash
make fmt          # ruff --fix + ruff format
make lint         # ruff check broker tests
make test         # pytest -q   (keep it green)
make docker-smoke # if you touched the image / runtime behavior
```

CI runs the same checks (`docs/ci.md`). `mypy` is advisory locally and in CI.

## Coding conventions

- Python, typed, `from __future__ import annotations`. Keep modules small and
  single-purpose; the manager drives the pure lifecycle state machine.
- Prefer clear, specific errors from `broker/errors.py` with a `remediation`.
- Add tests for every behavior change; security changes **must** have a test in
  `tests/test_security.py`.

## Security rules you must preserve

- **Never** build a shell command string. Launch child processes from an argv
  list via `security.build_argv` / `ProcessSupervisor`.
- Validate executables (`validate_executable`) and model paths
  (`validate_model_path`) against the configured allow-lists.
- Never let request data become a CLI flag; keep `reject_request_flags` on any
  request-derived value that can reach a process.
- Route all outbound HTTP through `validate_upstream_url` (SSRF guard).
- No silent model fallback. Unknown alias → `ModelNotFoundError`.
- Never commit secrets or host-specific details. `.env` is git-ignored; use
  placeholders in examples.
- Do not document unverified runtime flags as facts — mark vendor-sourced,
  unconfirmed behavior clearly (see the backend guides).

## Adding a backend

See `docs/backend-adapter.md`. Subclass `HTTPBackendAdapter` (or
`BackendAdapter`), register it in `broker/adapters/__init__.py`, add a registry
example, a backend guide under `docs/backends/`, and contract tests.

## Commits

Per project policy, commit **locally only**. Do not push, publish images, or
change external repository settings without explicit approval. Stage only
project files (`git status` should show no caches, `.venv`, or `.env`).
