# ADR 0004: The `auto` router (tiny request classifier)

- Status: Accepted
- Date: 2026-10-18
- Supersedes: none
- Related: ADR 0003 (routing & health), `docs/auto-router.md`, `broker/router.py`

## Context

Clients often don't know (or don't want to track) which of several registered
models best fits a given request. We want a single endpoint that picks the best
model per request from whatever the broker currently exposes — without a retrain
when the registry changes, without ever mis-serving (e.g. a text model for an
image request), and without adding heavy always-on dependencies to the base image.

## Decision

1. **Scope: a virtual `auto` alias only.** `{"model":"auto"}` is classified to a
   concrete model; every other id routes exactly as before. `auto` is listed in
   `/v1/models` (`backend: router`) and can be disabled (`router_backend=off`).

2. **Pipeline:** derive flags (`has_images`/`has_tools`/`needs_json`/
   `approx_ctx_tokens`) → **Stage-1 capability filter** (only models that can
   serve) → **data-driven hard overrides** → classifier → validate →
   **deterministic fallback** → LRU decision cache. The classifier only ever sees
   valid candidates; an empty candidate set is a clear error, not a mis-route.

3. **Two backends.** A zero-dependency **heuristic** scorer (capability match
   dominates; ties break cheaper → loaded → priority) is the default/fallback and
   the CI default. A **llama.cpp GGUF** classifier (0.5B–1.5B, Q4, CPU, resident)
   is used when available; decoding is constrained by a **GBNF grammar** to the
   exact candidate ids so output is 1–3 tokens and an invalid answer is
   impossible. `router_backend=auto|llm|heuristic|off`.

4. **Model-agnostic + data-driven knobs.** Candidates are injected at inference
   time, so adding/removing a model changes routing with no retrain. All tuning is
   data: per-model `tags`/`description`/`priority`/`cost_tier`/`router_eligible`,
   plus a registry `router` block (`default`, `policy`, `overrides`, `weights`).

5. **Safety + isolation.** Overrides can't select an incapable model; an
   invalid/empty classifier answer falls back to a capable default; the LLM runs
   in a worker thread and is serialized so it never blocks the event loop; a
   backend error degrades to the heuristic. No model loads at startup — the
   classifier loads lazily on first `auto` request and stays resident.

## Alternatives considered

- **Fixed-label classifier** (rejected): would need retraining whenever the model
  set changes; injecting candidates at inference time avoids that.
- **Free-form LLM output + parsing** (rejected): GBNF grammar makes invalid output
  impossible and keeps latency at a few tokens.
- **LLM-only** (rejected as default): adds a heavy dependency + model to the base
  image; the heuristic needs zero deps and is often better on small catalogs.
- **Routing every model through the classifier** (rejected): surprising and
  higher-latency; a dedicated `auto` alias is explicit and opt-in.

## Consequences

- The base image stays lean (heuristic only); the LLM classifier is an opt-in
  image variant (see ADR 0005).
- Classifier *quality* is model-dependent: a 0.5B can mis-rank on small catalogs
  where the heuristic is correct; a 1.5B improves it. Correctness (valid, capable
  choice) is guaranteed regardless by Stage-1 + grammar + fallback.
- Decisions are logged and surfaced at `/status.router` (backend, cache, recent
  decisions) for debugging and future tuning.
