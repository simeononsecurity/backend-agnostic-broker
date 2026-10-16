# Acceptance-Gate Evidence Ledger

This file maps each of the 22 acceptance gates to concrete, reproducible
evidence: file paths, commands, and observed results. All commands are run from
the repository root with the project virtualenv active
(`source .venv/bin/activate`) unless noted.

Environment used for the evidence below:

- macOS 26.5.2, Docker 28.3.0 (client + server), git 2.50.1
- Python 3.13.7 in `.venv` for local test runs; the Docker image uses
  `python:3.11-slim` (the supported floor, matching CI).
- 67 unit/integration/API tests, `ruff` clean, `ruff format --check` clean.

| # | Gate | Evidence |
|---|------|----------|
| 1 | Repository location recorded | `NOTES.md` → "Selected path: `~/Documents/GitHub/backend-agnostic-broker`". |
| 2 | Language choice has an ADR | `docs/adr/0001-language-choice.md` (Python 3.11+, with rationale vs Go/Rust/TypeScript). |
| 3 | Architecture review passes | `docs/architecture.md` (design) + `docs/adr/0002-architecture-review.md` (review record, decision: accepted). |
| 4 | All backends implement the adapter contract | `broker/adapters/base.py` defines the contract; `FakeAdapter` implements it directly; `ollama`/`vllm`/`llamacpp`/`strata` subclass `HTTPBackendAdapter`. Verified by `pytest tests/test_adapters.py`. |
| 5 | No model loads during startup | `broker/manager.py::ModelManager.validate_all()` only validates config; `create_app()` never starts a model. Verified by `pytest tests/test_lazy_load.py`. |
| 6 | A selected request loads only its model | `ModelManager.acquire()` → `get()` raises `ModelNotFoundError` (no silent fallback) then `ensure_ready()` on the one model. `pytest tests/test_lazy_load.py`. |
| 7 | Idle unload works | `ModelManager._sweep_once()` + `start_sweeper()`. `pytest tests/test_unload.py`. |
| 8 | `GET /v1/models` works | `broker/app.py` route; `pytest tests/test_api.py`. |
| 9 | `POST /v1/chat/completions` works | `broker/app.py` route; `pytest tests/test_api.py`. |
| 10 | Streaming works | SSE `StreamingResponse` in `app.py`; `pytest tests/test_streaming.py`. |
| 11 | Health and readiness checks work | `/healthz`, `/readyz` in `app.py`; `pytest tests/test_api.py`. |
| 12 | Authentication works | `broker/auth.py` (Bearer / X-API-Key, constant-time compare); `pytest tests/test_auth.py`. |
| 13 | Fake backend tests pass | `broker/adapters/fake.py`; `pytest tests/test_adapters.py tests/test_api.py`. |
| 14 | Security regression tests pass | `broker/security.py`; `pytest tests/test_security.py` (SSRF, path traversal, shell/flag injection). |
| 15 | Docker build passes | `docker build --build-arg VERSION=0.1.0 --build-arg VCS_REF=<sha> -t backend-agnostic-broker:0.1.0 .` → built `sha256:...`, image `backend-agnostic-broker:0.1.0`. |
| 16 | Docker Compose validation passes | `docker compose -f docker-compose.yml config -q` → rc 0; `docker compose -f docker-compose.yml -f docker-compose.gpu.yml config -q` → rc 0. |
| 17 | CI workflows pass locally | `ruff check broker tests` (All checks passed), `ruff format --check broker tests` (32 files already formatted), `python -m pytest -q` (67 passed), `python -m pytest -q tests/test_docs_examples.py` (4 passed). mypy is advisory (`|| true`) in CI. |
| 18 | PR jobs safe for forks | `.github/workflows/pr.yml`: trigger `pull_request` (not `pull_request_target`); `permissions: contents: read` default; per-job least privilege (only `build-scan` gets `security-events: write` for SARIF upload); third-party actions pinned to full commit SHAs; `concurrency … cancel-in-progress`. See `docs/secure-pr.md`. |
| 19 | Container scanning passes within policy | Policy: fail on **fixable** CRITICAL/HIGH only (`ignore-unfixed: true`). `artifacts/trivy-fixable.txt` → 0 findings. `artifacts/trivy-report.txt` → 44 HIGH / 0 CRITICAL, **all unfixed** (no fixed version available), therefore within policy. See `docs/security-model.md`. |
| 20 | Documentation examples work against the real API | `pytest tests/test_docs_examples.py` (4 passed) exercises the README/API-reference curl examples against the real FastAPI app with the fake backend. |
| 21 | No credentials or private host details in repo | Scanned all 53 git-tracked candidate files for IPs, private keys, AWS keys, `sk-` tokens, and host-specific paths: only match was a Debian package version string in `artifacts/sbom.syft.json`. `.env.example` contains placeholders only. `.venv`, caches are git-ignored. |
| 22 | Local git status shows only intended project files | `git add -A -n` lists exactly 53 project files (sources, tests, docs, Docker, CI, artifacts). `.gitignore` excludes `.venv/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `.env`. Commits are **local-only** per task constraints. |

## Reproduce the whole suite

```bash
make venv && make install
source .venv/bin/activate
ruff check broker tests
ruff format --check broker tests
python -m pytest -q                         # 67 passed
docker compose -f docker-compose.yml config -q
docker compose -f docker-compose.yml -f docker-compose.gpu.yml config -q
make docker-build                           # docker build, VERSION/VCS_REF labels
make docker-smoke                           # build + container smoke (healthz, 401, chat)
```

## Notes on the container scan policy (gate 19)

The image is `python:3.11-slim` (Debian 13). The 44 HIGH findings are all in
base-OS packages (`util-linux`, `ncurses`, `systemd`, `perl-base`, …) with **no
fixed version published** at scan time. The documented policy — enforced in CI
via Trivy `ignore-unfixed: true`, `severity: CRITICAL,HIGH`, `exit-code: 1` — is
to fail only on vulnerabilities that have a fix available. `trivy-fixable.txt`
shows zero such findings, so the gate passes. Operators should rebuild the image
periodically so base-image fixes are picked up as they are released.
