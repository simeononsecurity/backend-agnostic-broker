# ADR 0006: OpenAI endpoint surface — completions & responses via chat translation

- Status: Accepted
- Date: 2026-10-18
- Supersedes: none
- Related: `broker/openai_compat.py`, `broker/app.py`, `docs/api-reference.md`,
  competitive research (`docs/research/competitive-landscape.md`)

## Context

The broker served only `POST /v1/chat/completions`. Every comparable project
(llama-swap, LiteLLM, LocalAI, local-llm-router) also serves `/v1/completions`,
`/v1/embeddings`, and increasingly `/v1/responses`. The missing surface blocks
drop-in adoption by SDKs/agents that target those endpoints.

## Decision

We will expand the OpenAI surface. `/v1/completions` (legacy text completion) and
`/v1/responses` (Responses API) are implemented **by translating to and from the
existing chat pipeline** in a pure module (`broker/openai_compat.py`), so they
inherit all routing, pooling, lazy-load, failover, and the `auto` router with **no
backend/adapter changes**:

- `/v1/completions`: `prompt` → a single user message; chat.completion → a
  `text_completion` object; streaming chat chunks → `text_completion` chunks.
- `/v1/responses`: `input` (+ `instructions`) → chat messages; chat.completion →
  a `response` object (`output` / `output_text` / mapped usage); streaming →
  `response.output_text.delta` events + a terminal `response.completed`.

Both preserve the broker's invariants — the no-silent-fallback guarantee holds
(an unknown model still 404s), auth and request-size limits apply, and the model
selector passes the security flag check.

`/v1/embeddings` is also implemented, but **not** as a chat shim: unlike the
above it needs a new adapter-contract method (`embeddings()`) and real per-backend
integration (Ollama native `/api/embed`; OpenAI-compatible `/v1/embeddings` for
vLLM/llama.cpp). It routes by model name (no `auto` — nothing to classify) through
the same pool/lazy-load/failover machinery, and a chat-only model returns `400`.
It was verified against a live Ollama backend (nomic-embed-text, 768-dim) in
addition to the deterministic fake used in CI. `/v1/rerank` remains deferred and
is tracked in `docs/roadmap.md`.

## Alternatives considered

- **Proxy each endpoint straight to the backend.** Rejected: bypasses our
  routing/lifecycle/`auto` and multiplies per-backend compatibility work.
- **Implement embeddings/responses natively now across all adapters.** Partly
  adopted: responses/completions are translation shims; embeddings gets a real
  `embeddings()` adapter method with per-backend implementations, verified against
  a live backend rather than shipped as an untested stub.
- **Skip the Responses API.** Rejected: it is becoming the default for agent
  frameworks; a translation shim is cheap and forward-compatible.

## Consequences

- Drop-in clients using completions/responses now work unchanged.
- The shims are text-first: multimodal/tool-call fidelity on `/v1/responses` is
  best-effort (content parts are flattened to text); richer mapping can follow.
- A new `broker/openai_compat.py` is the single place for OpenAI⇄internal
  translation, unit-tested independently of backends.

## Confirmation

`tests/test_openai_endpoints.py` exercises completions/responses (non-stream +
stream), embeddings (single/batch/deterministic/validation), the 404 no-fallback
guarantee, and auth; `docs/openapi.yaml` lists the new paths. Embeddings was
additionally verified end-to-end against a live Ollama backend (nomic-embed-text,
768-dim vectors, matching the backend's direct output).
