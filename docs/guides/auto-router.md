# How-to: the `auto` router

Send `"model": "auto"` and let the broker pick the best model per request from
your live registry. The router is **data-driven** (per-model `tags`, `priority`,
`cost_tier`, `overrides`) and has two backends: a zero-dependency **heuristic**
(the lean image) and a tiny **GGUF classifier** (the self-contained `-llm` image).

## When to use

- Clients shouldn't hard-code model names; you want cheap requests on cheap models
  and hard ones on strong models.
- You want cost/latency/placement policy enforced centrally, not per client.

Skip `auto` for embeddings and rerank — those are addressed by name (there's
nothing to classify).

## Minimal config

Mark candidate models and give the router a deterministic fallback:

```yaml
# registry.yaml
router:
  default: llama3                 # used if the classifier returns an invalid choice
models:
  - { alias: tiny, backend: fake, runtime_model_id: t, tags: [general, cheap], cost_tier: 1 }
  - { alias: coder, backend: fake, runtime_model_id: c, tags: [code], cost_tier: 3 }
```

```bash
curl -s -D - -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{"model":"auto","messages":[{"role":"user","content":"write a python quicksort"}]}' \
  http://127.0.0.1:8080/v1/chat/completions
# response header x-broker-route shows the model actually served (or a cascade path)
```

## Full config (the knobs)

| Feature | Where | Notes |
|---|---|---|
| Backend | `BROKER_ROUTER_BACKEND=auto\|heuristic\|llm` | `llm` needs a GGUF (`-llm` image or `BROKER_ROUTER_MODEL_PATH`) |
| Scoring weights | `router.weights` in registry | capability dominates; raise `cost` / `prefer_loaded` / `priority` |
| Hard overrides | `router.overrides` | first matching rule wins, bypasses the classifier; skipped if the target can't serve |
| Cascade | `router.mode: cascade` | serve cheap, escalate once on a quality signal; `stream_mode: buffer` for streaming |
| Latency budget | `x-broker-latency-budget-ms` header / `BROKER_ROUTER_LATENCY_BUDGET_MS` | pick the fastest capable model that fits |
| Placement | `x-broker-placement` header / `BROKER_DEFAULT_PLACEMENT` + model `placement` labels | fail-closed: `403 placement_denied`, never spills |

The full semantics (every weight, cascade scorer, eval harness) live in the
reference: [auto-router.md](../auto-router.md).

## Verify

```bash
curl -s http://127.0.0.1:8080/status | python3 -c \
  'import sys,json; r=json.load(sys.stdin)["router"]; print(r["backend"], r.get("mode")); \
   [print(d) for d in r["recent_decisions"][-5:]]'
```

Expected: `recent_decisions` shows each pick (and any cascade path like
`tiny->coder`); the `x-broker-route` response header names the served model.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Everything routes to one model | Add distinguishing `tags`/`cost_tier`; check `router.weights`. |
| `auto` returns the fallback | The classifier returned an invalid id — check model aliases and `router.default`. |
| `403 placement_denied` | No candidate carries all required placement labels (by design). |
| LLM router not used | You're on the lean image; use the `-llm` image or set `BROKER_ROUTER_MODEL_PATH`. |

## Honesty note

The `auto` heuristic is a **cheap default**, not a strong classifier — its blind
generalization accuracy is modest (code 0.75 / math 0.70 / reasoning 0.44). For
stronger routing use `router_backend=llm` or `cascade`. All routing accuracy
numbers in the docs are labeled as simulated/offline evals.

## Example

[examples/auto-router](../../examples/auto-router/) — exercises tags, cascade,
latency budget, overrides, and placement offline.

## Reference & decisions

- Reference: [auto-router](../auto-router.md) · [model registry](../model-registry.md)
- ADRs: [0004 auto-router](../adr/0004-auto-router.md) ·
  [0011 auto-router evolution](../adr/0011-auto-router-evolution.md) ·
  [0019 router v2](../adr/0019-router-v2.md)
