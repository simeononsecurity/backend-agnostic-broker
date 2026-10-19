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
`pull_request`, so release credentials never exist in PR jobs. Default
permissions are `contents: read`; the job opts into `packages: write` to publish.
A tag run uses the workflow file **at the tagged commit**, so workflow changes
must be on the commit the tag points to. Steps:

- **Derive version + image ref** — bare semver from the tag (`v1.2.3` → `1.2.3`),
  lowercased `ghcr.io/<owner>/<repo>`.
- **Build + smoke + Trivy, twice** — the **lean** image (default build args) and
  the **LLM** image (`WITH_ROUTER_LLM=1`, bundled CPU classifier). Each is built
  `load: true, push: false`, smoke-tested (`scripts/docker_smoke.sh`), and gated
  by Trivy (`CRITICAL,HIGH`, `ignore-unfixed`, `exit-code 1`).
- **Publish** (only on a tag push, or dispatch with `publish=true`) — logs in to
  GHCR with `secrets.GHCR_PAT` if set, else the ephemeral `GITHUB_TOKEN`, and
  pushes the exact scanned images under four tags:
  `:X.Y.Z`, `:latest`, `:X.Y.Z-llm`, `:latest-llm`.

> **GHCR permission note:** publishing needs the repo's Actions token to have
> package write. If a push is denied (`permission_denied: write_package`) set
> repo Settings → Actions → General → Workflow permissions → "Read and write",
> or provide a `GHCR_PAT` secret (classic PAT with `write:packages`). Published
> packages are **private** by default.

All third-party actions are pinned to **full commit SHAs**.

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
