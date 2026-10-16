# Secure Pull-Request Workflow

The PR workflow (`.github/workflows/pr.yml`) is designed to run **untrusted
forked code safely**.

## Guarantees

- **`pull_request`, never `pull_request_target`.** Fork code runs in the
  restricted `pull_request` context: a read-only `GITHUB_TOKEN` and **no access
  to repository secrets**. `pull_request_target` (which would expose secrets to
  fork code) is never used for code execution.
- **Least privilege by default.** The workflow sets `permissions: contents:
  read`. Only the `build-scan` job elevates — and only to `security-events:
  write` — solely to upload the Trivy SARIF. No job gets write access to repo
  contents.
- **Pinned actions.** Every third-party action is pinned to a full commit SHA
  (not a mutable tag), preventing supply-chain tag-moving attacks.
- **No secret exposure.** Because the trigger is `pull_request`, secrets are not
  injected; the gitleaks job uses only the default `GITHUB_TOKEN`.
- **Concurrency cancellation.** Superseded runs for the same PR are cancelled
  (`cancel-in-progress: true`), limiting resource abuse.
- **Image is built, not pushed.** `build-scan` builds with `push: false`;
  publishing lives only in the release workflow behind a protected environment.

## Container-scan policy

Trivy runs with `severity: CRITICAL,HIGH`, `ignore-unfixed: true`,
`exit-code: 1` — the PR fails on any **fixable** critical/high vulnerability, but
does not block on base-OS CVEs that have no published fix. Those are cleared by
rebuilding against updated base images (see `docs/EVIDENCE.md` gate 19).

## Release credentials isolation

Release signing/publishing credentials exist only in `release.yml`, which never
runs on `pull_request`. Publishing additionally requires an explicit opt-in and
a protected `release-approval` environment with required reviewers.
