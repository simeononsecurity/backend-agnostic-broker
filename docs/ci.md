# CI Guide

Two GitHub Actions workflows under `.github/workflows/`.

## PR workflow (`pr.yml`)

Triggered on `pull_request` to `main`. Default permissions are `contents: read`;
`concurrency` cancels superseded runs. Jobs:

- **lint-test** — install `-e ".[dev]"`, `ruff check`, `ruff format --check`,
  `mypy` (advisory, non-blocking), `pytest -q`, the docs-example test, and
  uploads test logs.
- **compose-validate** — validates the base and GPU-overlay Compose files with
  `docker compose config`.
- **secrets-scan** — gitleaks secret scan over full history.
- **build-scan** (needs `lint-test`) — builds the CPU image with Buildx,
  runs the container smoke test, generates an SBOM (SPDX JSON via
  `anchore/sbom-action`), scans the image with Trivy (`severity: CRITICAL,HIGH`,
  `ignore-unfixed: true`, `exit-code: 1`), uploads the Trivy SARIF
  (`security-events: write`, the only elevated permission), and uploads the SBOM
  + reports. 

All third-party actions are pinned to **full commit SHAs**.

## Release workflow (`release.yml`)

Runs **only** on `v*.*.*` tags (and manual `workflow_dispatch`); never on
`pull_request`, so release credentials never exist in PR jobs. Jobs:

- **build** — derives an immutable version from the tag, builds the image
  (`load: true`, `push: false`), runs the container smoke test, and applies the
  Trivy gate.
- **publish** — gated behind BOTH an explicit `inputs.publish == true` and a
  protected `release-approval` environment (required reviewers). It is a no-op
  placeholder until a registry is configured; **images are not published by
  default**, matching the task's hard constraint.

## Reproduce CI locally

```bash
source .venv/bin/activate
ruff check broker tests
ruff format --check broker tests
python -m pytest -q
docker compose -f docker-compose.yml config -q
docker compose -f docker-compose.yml -f docker-compose.gpu.yml config -q
make docker-build && make docker-smoke
```

See `docs/secure-pr.md` for why the PR workflow is safe for forked code.
