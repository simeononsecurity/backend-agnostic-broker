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

## Advanced routing (ADR 0011 + 0019, all opt-in)

These features are **off by default**; with none configured, `auto` behaves
exactly as above. Design rationale and prior art: [ADR 0011](adr/0011-auto-router-evolution.md),
[ADR 0019](adr/0019-router-v2.md), and the research notes under
[docs/research/](research/).

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
`tiny->coder`).

#### Calibrated reliability scorer (ADR 0019, opt-in)

Instead of the fixed booleans, set `router.cascade.scorer.enabled: true` to reduce
the cheap hop's answer to one **reliability score** in `[0, 1]` and escalate when
`score < threshold`. The score combines **graded** features (answer-length adequacy
and, when the response carries token logprobs, mean token probability) with **hard
safety gates** (a refusal match or a truncation caps the score to 0, exactly like
the booleans). This gives one calibrated knob — slide `threshold` up to demand more
certainty (escalate more), down to let the cheap model carry more load.

```yaml
router:
  mode: cascade
  cascade:
    scorer:
      enabled: true
      threshold: 0.5        # escalate when reliability < 0.5
      target_chars: 200     # answer length at which the length feature saturates
      weight_length: 1.0
      weight_logprob: 0.0   # >0 uses token logprobs when the response has them
      gate_refusal: true    # a refusal always escalates
      gate_truncation: true # finish_reason == "length" always escalates
```

The scorer is **off by default**; disabled, the boolean signals above are used
unchanged. It is a heuristic aggregator (one tunable threshold + an optional logprob
signal), not a trained reliability model — see [ADR 0019](adr/0019-router-v2.md) for
why the trained FrugalGPT-style scorer was rejected.

#### Streaming: fallback-to-single (default) or buffer (ADR 0019)

A live stream cannot be re-scored, so `router.cascade.stream_mode` decides:

- **`fallback_single`** (default, = v0.5.0): a streaming cascade request is served
  by the single strong pick; the route header reads `strong (cascade->single:
  streaming)`.
- **`buffer`**: the **chat** endpoint buffers the cheap hop fully, scores it, then
  **replays** the buffered answer as a stream (kept path) or **streams the stronger
  hop live** (escalated path). Only one model's output is ever emitted into a
  stream — never a mix. The kept path pays the cheap model's full generation time as
  perceived TTFT (inherent to buffering). The route header reads
  `tiny->strong (cascade:buffer)`; the executed path is in `GET /status`. The
  completions/responses streaming endpoints always use `fallback_single`.

```yaml
router:
  mode: cascade
  cascade:
    stream_mode: buffer   # chat streaming cascade; default is fallback_single
```

### 2. Latency budget

Send `x-broker-latency-budget-ms: 2000` (or set `router.latency_budget_ms` /
`BROKER_ROUTER_LATENCY_BUDGET_MS`). The router estimates each candidate's latency
from the usage log (ADR 0007) — p90 over `BROKER_ROUTER_LATENCY_WINDOW_SECONDS`,
plus `BROKER_ROUTER_COLD_START_MS` when the model is not currently resident — and,
if the chosen model would exceed the budget, picks the fastest capable candidate
instead. It **never** violates the Stage-1 capability filter or placement.

**TTFT for streaming (ADR 0019).** For a **streaming** request the estimate uses
**time-to-first-token** (the latency a streaming client actually perceives before
the first token), summarized only over prior *streamed* samples; for a
**non-streaming** request it uses **end-to-end** latency. A model with no streamed
samples yet has no TTFT estimate and is simply not ranked by the budget (rather than
treated as instant). TTFT is recorded automatically for every streaming request.

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
(`tests/data/router_eval.jsonl`, 82 prompts, plus a frozen held-out split
`tests/data/router_eval_heldout.jsonl`, 24 prompts) instead of vibe-checking it. It
runs the **heuristic** backend offline (no GPU/network) and optionally the LLM
backend when a GGUF is available. It reports two families of numbers: **direct
metrics** (always valid) and **RouteLLM metrics** (PGR / APGR / CPT), each computed
both from the **router's own swept cost–quality curve** and from an **oracle upper
bound** derived from the labels. The exact definitions and citations are in the
research notes (`docs/research/adr-0011-auto-router-evolution.md`,
`docs/research/adr-0019-router-v2.md`).

```bash
python scripts/router_eval.py                                        # train set
python scripts/router_eval.py --dataset tests/data/router_eval_heldout.jsonl
python scripts/router_eval.py --format json   # metrics as JSON
python scripts/router_eval.py --backend llm   # use the GGUF classifier
```

A CI run of the harness on the heuristic backend over the bundled **82-prompt
train** set (`tests/data/router_eval.jsonl`) with the improved ADR 0019 heuristic:

| Direct metric | Value |
|---------------|-------|
| Prompts | 82 |
| Accuracy (chosen is acceptable) | 1.000 |
| Cheapest-capable accuracy | 0.842 |
| Mean cost_tier (router) | 2.878 |
| Mean cost_tier (oracle / cheapest-capable) | 2.573 |
| Router strong-call fraction (top tier) | 0.146 |

| RouteLLM metric | Router curve | Oracle upper bound |
|-----------------|--------------|--------------------|
| r(M_w) / r(M_s) | 0.220 / 1.000 | — |
| PGR (operating point) | 1.000 | — |
| APGR | 0.789 | 0.834 |
| CPT(50%) | 0.335 | — |
| CPT(80%) | 0.335 | — |

#### ADR 0019 code/math improvement — before vs after (train and frozen held-out)

The heuristic's cost term is now **normalized** so it is a tie-breaker that cannot
outweigh a single capability match, and the greedy `"write a"` creative keyword was
removed (it mis-tagged code prompts). The labeled set grew to **82 train** prompts
plus a **frozen 24-prompt held-out** split (`tests/data/router_eval_heldout.jsonl`)
that is **not used for tuning**. Per-task accuracy, old heuristic → new heuristic:

| Task | Train before | Train after | Held-out before | Held-out after |
|------|--------------|-------------|-----------------|----------------|
| code | 0.636 | **1.000** | 0.500 | **1.000** |
| math | 0.091 | **1.000** | 0.000 | **1.000** |
| chat | 1.000 | 1.000 | 1.000 | 1.000 |
| reasoning | 0.833 | 1.000 | 1.000 | 1.000 |
| creative | 1.000 | 1.000 | 1.000 | 1.000 |
| tools / json | 1.000 | 1.000 | 1.000 | 1.000 |
| vision / long-context | 1.000 | 1.000 | 1.000 | 1.000 |
| **overall accuracy** | **0.768** | **1.000** | 0.792 | **1.000** |

No held-out task regresses (every task is flat or improves); code and math rise
sharply on both splits. The accuracy thresholds are pinned by
`tests/test_router_v2.py` (code ≥ 0.90 / 0.80, math ≥ 0.90 / 0.80 on train /
held-out), and the held-out split is enforced disjoint-from-train by a test. The
cost of capability-first routing is a higher mean cost_tier (2.57 → 2.88 on train);
the swept cost–quality curve still spans the full range, so APGR/CPT stay
meaningful (lowering `router.weights.cost` no longer *raises* accuracy — the default
operating point already reaches the ceiling on this set).

#### What the metrics mean (and the limits of label-based evaluation)

- **Quality is binary capability-acceptability.** `s(q, m) = 1` iff model `m` is
  in the prompt's `acceptable` list. "Accuracy" is the mean of `s` over the
  router's picks. This measures *did the router pick a model that can serve the
  request*, not answer quality — the labels encode capability, not graded merit.
- **The strong reference is a true ceiling, not a single model.** RouteLLM's
  two-model setup uses one strong model `M_s` that dominates the weak one. No
  single concrete model here is capable across every task (e.g. `reasoner` cannot
  do vision), so the most-expensive model is *not* a ceiling — using it produced
  `r(M_s) < accuracy` and a meaningless `PGR` clamped to 1.000. We instead define
  `r(M_s) = mean_q max_m s(q, m)` — the accuracy of perfect (oracle) routing — so
  `r(M_s) ≥ r(any router)` and `PGR ∈ [0, 1]` by construction.
- **APGR/CPT evaluate the router, via a swept knob.** They are integrated over
  the router's *own* curve (sweeping `router.weights.cost`), with normalized mean
  cost as the x-axis. The oracle frontier is the cost-optimal label bound and is
  reported separately as an upper bound, never under the bare name APGR.
- **No silent clamping.** PGR is reported raw; if the quality gap
  `r(M_s) − r(M_w) ≤ 0`, or a value is out of `[0, 1]`, the metric is marked
  `invalid` with a reason in both the markdown and JSON output.
- **Limitations.** Numbers are only as good as the labels: small set (45),
  hand-labeled capability (not human-graded quality), and a synthetic 6-model
  catalogue. Treat them as a regression signal for routing *changes*, not an
  absolute quality score. The LLM backend and real registries will shift them.
