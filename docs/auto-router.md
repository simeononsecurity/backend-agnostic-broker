# The `auto` router

> Design rationale and alternatives: [ADR 0004](adr/0004-auto-router.md).

`auto` is a virtual model id. Send a request with `"model": "auto"` and a tiny
classifier picks the single best real model the broker currently exposes, then
the request flows through normal pool routing + failover. **Only `auto` is
affected** — every other model id is addressed directly as before.

```bash
curl -s localhost:8080/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -d '{"model":"auto","messages":[{"role":"user","content":"debug my python function"}]}'
# -> served by whichever model is tagged for code; response "model" is that model's id
```

## How a choice is made

```
extract flags  ->  Stage-1 capability filter  ->  hard overrides (data)
               ->  classifier (LLM or heuristic)  ->  validate  ->  fallback  ->  cache
```

1. **Flags** are derived from the request: `has_images` (vision parts),
   `has_tools` (a `tools` array or tool messages), `needs_json`
   (`response_format` json), and `approx_ctx_tokens` (message size + `max_tokens`).
2. **Stage-1 capability filter** keeps only models that *can* serve the request:
   vision for images, tool support for tool calls, and a context window large
   enough. The classifier only ever sees valid candidates. If none qualify the
   request fails with a clear error (no silent mis-route).
3. **Hard overrides** (registry `router.overrides`) are checked in order; the
   first match whose target is a valid candidate is used and the LLM is skipped.
4. **Classifier** picks one id from the candidates (LLM or heuristic, below).
5. **Validate + fallback**: if the choice isn't a valid candidate, the broker
   uses the deterministic default (`router.default` / `BROKER_ROUTER_DEFAULT_MODEL`
   if it's a valid candidate, else the best heuristic pick — always capable).
6. **Cache**: decisions are memoized by `(flags, candidate set, prompt prefix)`.

Every decision is logged and the last ones are visible at `GET /status` →
`router.recent_decisions` (`chosen`, `via`, `flags`, `latency_ms`).

## Classifier backends (`BROKER_ROUTER_BACKEND`)

- **`auto`** (default) — use the llama.cpp classifier when its model + library
  are present, else the heuristic.
- **`heuristic`** — a pure-Python scorer (no model, no extra deps). Capability
  match dominates; ties break toward cheaper → already-loaded → higher priority.
  This is the default in CI and the fallback when the LLM is unavailable.
- **`llm`** — force the llama.cpp classifier (startup error if unavailable).
- **`off`** — disable the `auto` alias entirely (it won't appear in `/v1/models`;
  a request to `auto` returns 404).

### The tiny LLM classifier

A small instruction-tuned GGUF (0.5B–1.5B, Q4) via `llama-cpp-python`, CPU-only,
loaded once and kept resident. Decoding is constrained by a **GBNF grammar**
restricted to the exact candidate ids, so the output is 1–3 tokens and an
invalid/unparseable answer is impossible. The prompt is a short flags line + a
compact candidate table (id, tags, caps, ctx, cost, loaded) + the truncated last
user message — not full model cards. It never hard-codes model names: the
candidate list is injected at inference time, so adding/removing a registry model
instantly changes routing with no retrain.

Bundle it in the container (see [docker.md](docker.md)):

```bash
docker build --build-arg WITH_ROUTER_LLM=1 -t broker:router .
# installs the CPU llama-cpp-python wheel + downloads a small Q4 GGUF to
# /models/router.gguf and sets BROKER_ROUTER_MODEL_PATH. router_backend=auto then
# uses it; without it the heuristic is used.
```

Good base models: Qwen2.5-0.5B/1.5B-Instruct, Llama-3.2-1B-Instruct, SmolLM2.

## Customization (three knobs, all data)

1. **Per-model tags / metadata** (registry entry) — the main control surface:
   `tags` (e.g. `code`, `vision`, `reasoning`, `long-context`, `tools`, `json`),
   `description`, `priority`, `cost_tier` (1 cheap .. 5 expensive),
   `router_eligible` (set false to hide a model from `auto`).
2. **Policy** (`router.policy`) — the system prompt / routing policy for the LLM
   (how to weigh cost vs capability vs prefer-loaded).
3. **Hard overrides** (`router.overrides`) — deterministic `when → use` rules
   that bypass the LLM, plus `router.default` and scoring `router.weights`.

```yaml
# registry.yaml
router:
  default: small-chat            # deterministic fallback
  weights: { capability: 3.0, cost: 1.0, prefer_loaded: 1.0, priority: 1.0 }
  overrides:
    - when: { has_images: true }      # always send images to the vision model
      use: llava
    - when: { regex: "(?i)\\btranslate\\b" }
      use: small-chat
    - when: { min_ctx_tokens: 32000 }
      use: long-ctx
models:
  - alias: coder
    backend: ollama
    runtime_model_id: qwen2.5-coder:7b
    tags: [code, reasoning]
    description: strong at code & debugging
    cost_tier: 3
    priority: 1
  # ...
```

An override whose target is **not** a valid Stage-1 candidate (e.g. it can't do
vision for an image request) is skipped, so overrides can never force an
incapable model.

## Settings

| Env var | Default | Meaning |
|---|---|---|
| `BROKER_ROUTER_ENABLED` | `true` | expose the `auto` alias |
| `BROKER_ROUTER_ALIAS` | `auto` | the virtual model id that triggers routing |
| `BROKER_ROUTER_BACKEND` | `auto` | `auto` / `llm` / `heuristic` / `off` |
| `BROKER_ROUTER_MODEL_PATH` | _(unset)_ | GGUF for the LLM classifier (set in-container) |
| `BROKER_ROUTER_MODEL_CTX` | `2048` | classifier context window |
| `BROKER_ROUTER_THREADS` | _(auto)_ | CPU threads for the classifier |
| `BROKER_ROUTER_MAX_INPUT_CHARS` | `2048` | last-user-message truncation (~512 tokens) |
| `BROKER_ROUTER_CACHE_SIZE` | `512` | decision cache entries (LRU) |
| `BROKER_ROUTER_TIMEOUT_SECONDS` | `10` | per-classification timeout |
| `BROKER_ROUTER_DEFAULT_MODEL` | _(unset)_ | fallback model id (overrides `router.default`) |

## Safety rails

- Stage-1 capability filter runs **before** the classifier — it only ever sees
  valid candidates.
- An invalid/empty classifier answer falls back to a deterministic default
  (always a capable model).
- Overrides can't select an incapable model.
- The LLM classifier runs in a worker thread and is serialized, so it never
  blocks the event loop; a backend error falls back to the heuristic.
- No model loads at broker startup — the classifier model loads lazily on first
  `auto` request and stays resident.
