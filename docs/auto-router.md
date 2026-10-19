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

### Run it outside the container

On a non-container deploy, install the CPU wheel and point the broker at a GGUF:

```bash
pip install llama-cpp-python          # needs a C/C++ toolchain to build; libgomp at runtime
export BROKER_ROUTER_MODEL_PATH=/path/to/router.gguf
export BROKER_ROUTER_BACKEND=llm      # force the LLM (auto would also pick it up)
# optional: BROKER_ROUTER_THREADS, BROKER_ROUTER_MODEL_CTX
```

### Verify it's really using the LLM

`router.backend` reports the *configured* backend; a decision can still fall back
to the heuristic if the model fails to load. Check **both** the backend and the
`via` of a real decision:

```bash
curl -s localhost:8080/status \
  | python3 -c 'import sys,json;r=json.load(sys.stdin)["router"];print(r["backend"], r["recent_decisions"][-1]["via"] if r["recent_decisions"] else "-")'
# want:  llm llm      (not  llm heuristic)
```

A common cause of a silent `via: heuristic` fallback is a missing OpenMP runtime
(`libgomp.so.1`) for the compiled wheel — the `-llm` image installs it; a custom
build must too.

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
| `BROKER_ROUTER_LATENCY_BUDGET_MS` | _(unset)_ | default latency budget (ms); header/registry win first |
| `BROKER_ROUTER_LATENCY_WINDOW_SECONDS` | `3600` | usage-log window for the latency estimate |
| `BROKER_ROUTER_COLD_START_MS` | `1500` | cold-start penalty for a not-resident model |
| `BROKER_DEFAULT_PLACEMENT` | `[]` | baseline placement labels required of every request |

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

## Advanced routing (ADR 0011, all opt-in)

These four features are **off by default**; with none configured, `auto` behaves
exactly as above. Design rationale and prior art: [ADR 0011](adr/0011-auto-router-evolution.md)
and [docs/research/adr-0011-auto-router-evolution.md](research/adr-0011-auto-router-evolution.md).

### 1. Cascade mode (cheap → escalate once)

Set `router.mode: cascade` to serve the **cheapest capable** model first, inspect
its *actual* answer, and escalate **once** to a stronger capable model only when a
signal fires (FrugalGPT-style). Signals are data-driven (`router.cascade`):
`finish_reason == "length"` (truncation), empty/too-short output (`min_chars`), or
a refusal regex (`refusal_patterns`) — each individually toggleable.

```yaml
router:
  mode: cascade
  cascade:
    escalate_on_length: true
    escalate_on_empty: true
    min_chars: 1
    escalate_on_refusal: true
```

Both hops are recorded (usage log + `GET /status` → `router.recent_decisions`),
and the response header **`x-broker-route`** shows the path (`tiny` or
`tiny->coder`). **Streaming**: a live stream cannot be cheaply re-scored, so a
streaming cascade request **falls back to the single strong pick** and the route
header reads `strong (cascade->single: streaming)`.

### 2. Latency budget

Send `x-broker-latency-budget-ms: 2000` (or set `router.latency_budget_ms` /
`BROKER_ROUTER_LATENCY_BUDGET_MS`). The router estimates each candidate's latency
from the usage log (ADR 0007) — p90 over `BROKER_ROUTER_LATENCY_WINDOW_SECONDS`,
plus `BROKER_ROUTER_COLD_START_MS` when the model is not currently resident — and,
if the chosen model would exceed the budget, picks the fastest capable candidate
instead. It **never** violates the Stage-1 capability filter or placement. The
estimate is end-to-end latency (not time-to-first-token).

### 3. Placement / data-residency pinning (fail-closed)

Tag models with `placement` labels in the registry (e.g. `[local, trusted]`,
`[region:eu]`). Send `x-broker-placement: region:eu` (and/or set a baseline with
`BROKER_DEFAULT_PLACEMENT`). Only models/pool members carrying **all** required
labels are eligible — for `auto`, for an explicit model, and for pool members. If
nothing qualifies the broker returns **`403 placement_denied`** and never spills to
a non-compliant model. See [security-model.md](security-model.md).

```bash
curl -s localhost:8080/v1/chat/completions -H "Authorization: Bearer ***" \
  -H "x-broker-placement: region:eu" \
  -d '{"model":"auto","messages":[{"role":"user","content":"hello"}]}'
# -> served only by a model tagged region:eu, or 403 placement_denied
```

### 4. Evaluation harness

`scripts/router_eval.py` measures routing quality against a labeled set
(`tests/data/router_eval.jsonl`, ≥40 prompts) instead of vibe-checking it. It runs
the **heuristic** backend offline (no GPU/network) and optionally the LLM backend
when a GGUF is available, computing accuracy, cheapest-capable match, cost
weighting, and RouteLLM's **PGR / APGR / CPT(50%,80%)** against an oracle
cost–quality frontier derived from the labels (the deterministic, multi-model
adaptation of RouteLLM's definitions is documented in the research note).

```bash
python scripts/router_eval.py                 # markdown report (heuristic backend)
python scripts/router_eval.py --format json   # metrics as JSON
python scripts/router_eval.py --out report.md # write the report
python scripts/router_eval.py --backend llm   # use the GGUF classifier
```

A CI run of the harness on the heuristic backend over the bundled 45-prompt set:

| Metric | Value |
|--------|-------|
| Prompts | 45 |
| Accuracy (chosen is acceptable) | 0.867 |
| Cheapest-capable accuracy | 0.822 |
| Mean cost_tier (router) | 2.29 |
| Mean cost_tier (oracle) | 2.49 |
| Weak-only accuracy r(Mw) | 0.267 |
| Strong-only accuracy r(Ms) | 0.622 |
| PGR (router operating point) | 1.000 |
| APGR (oracle frontier) | 0.822 |
| CPT(50%) strong-call fraction | 0.200 |
| CPT(80%) strong-call fraction | 0.289 |
| Router strong-call fraction | 0.022 |

Per-task accuracy exposes real weaknesses — e.g. `math` ≈ 0.33 and `code` ≈ 0.60
with the default weights, because a single tag match (+capability) can be
outweighed by the cheap-model cost bonus. That is exactly the kind of regression
the harness exists to surface (the motivating `tiny`-for-code mis-route); tune
`router.weights` or add overrides and re-run to measure the effect.
