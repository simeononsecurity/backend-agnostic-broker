# `auto` router example

A complete, runnable `auto`-router registry that exercises every router feature:
per-model **tags / priority / cost tiers**, a **cascade** with the **calibrated
reliability scorer** and the **buffered streaming** mode, a **latency budget**,
hard **overrides**, and fail-closed **placement** labels (ADR 0004 / 0011 / 0019).

The models use `backend: fake`, so this example runs offline with no GPU or network
(the bundled test, `tests/test_example_auto_router.py`, serves real requests against
it). In a real deployment swap each `backend` / `runtime_model_id` / `base_url` for
your Ollama / vLLM / llama.cpp model — the whole `router` block stays the same.

## Run it

```bash
export BROKER_REGISTRY_PATH=examples/auto-router/registry.yaml
export BROKER_ENABLED_BACKENDS=fake            # use your real backends in production
export BROKER_AUTH_ENABLED=false               # demo only
uvicorn broker.app:create_app --factory --port 8080
```

## Send a request to `auto`

```bash
curl -s localhost:8080/v1/chat/completions \
  -d '{"model":"auto","messages":[{"role":"user","content":"write a python function to reverse a linked list"}]}' \
  -D -            # -D - prints the response headers
# -> served by `coder` (tagged for code). Read the route from the headers:
#    x-broker-route: coder                  (single pick)
#    x-broker-route: tiny->coder            (cascade escalated)
#    x-broker-route: tiny->coder (cascade:buffer)   (buffered streaming cascade)
```

- **`x-broker-route`** (response header) is the model actually served, or the
  cascade path. For a streaming request in `stream_mode: buffer` it shows the plan
  (`cheap->strong (cascade:buffer)`); the *executed* hop is in `/status`.
- **Latency budget**: add `-H 'x-broker-latency-budget-ms: 1500'` to cap latency
  (TTFT for streaming, end-to-end otherwise); the router picks the fastest capable
  model that fits.
- **Placement**: every model here is tagged `local`. Add `-H 'x-broker-placement:
  local'` to require it; a request requiring a label no model carries gets
  `403 placement_denied` (never a spill to a non-compliant model).

## Inspect routing decisions

```bash
curl -s localhost:8080/status | python3 -c \
  'import sys,json; r=json.load(sys.stdin)["router"]; print(r["backend"], r["mode"]); \
   [print(d) for d in r["recent_decisions"][-5:]]'
# Shows the backend (heuristic/llm), the mode (cascade), and the last decisions,
# including each cascade path (e.g. {"cascade": {"path": "tiny->coder", ...}}).
```

## Run the eval harness

The eval harness scores the router's **single pick** per prompt. In `cascade` mode
that pick is the cheap *primary* hop (the escalation is executed at serve time, not
simulated by the offline harness), so run it against a **single-mode** copy to
measure capability routing — or just use the bundled labeled set, whose catalogue
matches these aliases:

```bash
python scripts/router_eval.py          # bundled 82-prompt train set (single-mode eval catalogue)
# To score THIS registry's capability routing, set `mode: single` first, since the
# offline harness does not replay cascade escalation.
```

See [docs/auto-router.md](../../docs/auto-router.md) and
[ADR 0019](../../docs/adr/0019-router-v2.md) for the full semantics.
