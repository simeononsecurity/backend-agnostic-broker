# ADR 0005: Container image variants and GHCR publishing

- Status: Accepted
- Date: 2026-10-18
- Supersedes: none
- Related: ADR 0004 (auto-router), `docs/docker.md`, `docs/ci.md`, `Dockerfile`,
  `.github/workflows/release.yml`

## Context

The `auto` router has an optional CPU LLM classifier (ADR 0004) that requires
`llama-cpp-python` (a C++ compile) plus a model file. Bundling that into every
image would bloat it (~1.35 GB) and slow/risk the release pipeline, but some users
want a fully self-contained in-container classifier. We also needed the final
image to carry neither compilers nor a wheels layer.

## Decision

1. **Two published variants per release.**
   - **lean** (default build): broker only; `auto` uses the heuristic. ~272 MB.
     Tags `:X.Y.Z`, `:latest`.
   - **llm** (`--build-arg WITH_ROUTER_LLM=1`): adds the CPU `llama-cpp-python`
     package + a small Q4 GGUF at `/models/router.gguf` (+ `libgomp1` runtime).
     ~1.35 GB. Tags `:X.Y.Z-llm`, `:latest-llm`. The model URL is overridable via
     `--build-arg ROUTER_MODEL_URL=...`.

2. **Three-stage build.** `pydeps` installs Python deps into a relocatable
   `/install` prefix; `router` (optional) compiles the wheel and fetches the GGUF;
   `runtime` copies only the **installed packages** (and, for llm, the model). The
   compile toolchain (`build-essential`/`cmake`) and all build wheels stay in build
   stages — the final image has no compiler and no wheels layer. `BROKER_ROUTER_MODEL_PATH`
   is set to the bundled path; if the file is absent (lean) the router falls back
   to the heuristic automatically.

3. **Release publishing.** `release.yml` builds, smoke-tests, and Trivy-gates both
   variants, then pushes all four tags to GHCR. Login prefers a `GHCR_PAT` secret
   (classic PAT, `write:packages`) and falls back to the repo `GITHUB_TOKEN`.

## Alternatives considered

- **Bundle the LLM in the only image** (rejected): bloats every pull and slows CI.
- **Disable OpenMP in the llama.cpp build to avoid `libgomp`** (rejected): loses
  CPU parallelism; installing `libgomp1` in the llm runtime is cheaper and keeps
  inference fast.
- **Copy the wheels dir into runtime then `rm`** (rejected): the COPY layer
  persists in the image; copying the installed prefix avoids the bloat entirely.

## Consequences

- The lean image stays apt-free and small; the llm variant is opt-in.
- Two builds roughly double release-job time (the llm build compiles llama.cpp and
  downloads the model); acceptable for a tag-only workflow.
- Published GHCR packages are **private** by default; visibility is a manual,
  user-controlled setting.
- First-time GHCR publishing required granting the Actions token package-write
  (see `docs/ci.md`); resolved via a `GHCR_PAT` secret.
