# How-to: upgrading from 0.4 / 0.5

**Nothing changes by default.** Every feature added since 0.5.0 (rerank, semantic
cache, web UI, governance v2, reliability v2, cache-aware v2, router v2) is
**opt-in and default-off**, and the v0.5.0 configuration reproduces v0.5.0 behavior
byte-for-byte. Upgrading is: update the package/image, keep your config, done.

This guide lists the only behavior changes you should be aware of, and how to
adopt the new features when you want them.

## Upgrade steps

```bash
# From source:
git pull && pip install -e '.[dev]'     # add ,redis / ,otel if you use those extras
# Docker:
docker pull ghcr.io/simeononsecurity/backend-agnostic-broker:latest
```

Your existing `registry.yaml`, `keys.yaml`, and `.env` keep working unchanged.

The new features added alongside this branch — GPU-safe model swapping
(ADR 0020/0022), request queueing (ADR 0023), and crash recovery / restart
reconciliation (ADR 0021) — are likewise **opt-in and default-off**, so none of
them changes behavior unless you enable them (see the adopting table below).

## The three behavior changes to know

These are the **only** non-default-off changes since v0.5.0. Both are documented in
`CHANGELOG.md`.

### 1. Upstream HTTP errors are surfaced by status (ADR 0017)

Previously any backend error collapsed to `502 backend_unavailable` and was retried
across every pool member. Now:

- A backend **4xx** (e.g. a context-length `400`) returns that **4xx** with
  `error.type: upstream_client_error`, is called **exactly once**, and **never
  benches** a member.
- A backend **429/503** fails over to a healthy member immediately and benches the
  throttled one.
- Transport failures (connect/read timeout, unreachable) remain `502`.

**Impact:** clients that treated every upstream failure as `502` will now see the
real `4xx`. This is strictly more correct — handle `4xx` as a client error. See
[api-reference.md](../api-reference.md#upstream-backend-errors-adr-0017).

### 2. Cache-aware default β changed 0.25 → 0.5 (ADR 0018)

Only affects deployments that opted into `BROKER_ROUTE_STRATEGY=cache_aware`
**without** setting `BROKER_CACHE_AWARE_BETA`. The default was re-derived against a
queueing benchmark (see [routing-and-health.md](../routing-and-health.md)). To keep
the old value, set `BROKER_CACHE_AWARE_BETA=0.25` explicitly. `sticky` remains the
default **strategy**, so most deployments are unaffected.

### 3. `auto` scoring: `cost_normalized` defaults to true (ADR 0019)

`router.weights.cost_normalized` is now `true`. In v0.5.0, once the cheapest model
was warm, its cost bonus plus `prefer_loaded` could outweigh a capability match, so
`auto` sent code/math/reasoning prompts to it. You may see **more** such prompts go
to capable (pricier) models. To keep v0.5.0 scoring:

```yaml
router:
  weights: { cost_normalized: false }
```

### 4. `gpu.required` is now enforced in every adapter (ADR 0025)

Previously only the fake test backend read `gpu.required`; the production adapters
ignored it, so a required model could silently run on CPU or on the wrong device.
It is now enforced in the admission path for any entry that sets `gpu.required`
(models **without** it are unchanged). This is **on by default** — it closes a
documented promise, not an opt-in feature.

- A `gpu.required` entry with **no resolvable GPU** (telemetry off and no declared
  inventory) now fails the load with **503** `gpu_unavailable` instead of running
  on CPU. Declare the inventory with `BROKER_GPU_DEVICES=0,1` (or
  `BROKER_CUDA_VISIBLE_DEVICES`), or turn on `BROKER_GPU_TELEMETRY`.
- A `gpu.device_ids` pointing at a device **not in the inventory** now fails the
  load with 503 `gpu_unavailable`.
- Setting `gpu.required: true` on an **external** OpenAI-compatible endpoint (no
  `startup_command`, non-Ollama) the broker cannot verify is now a **startup
  config error**. Add `gpu.verify: declared` to assert placement out-of-band (it
  shows as `declared` in `/status`), or remove `gpu.required`.
- After a successful load, broker-managed runtimes (with telemetry) and Ollama are
  verified to actually hold GPU memory; a miss unloads the model and returns
  **503** `gpu_placement_failed` within `BROKER_GPU_VERIFY_SECONDS` (default 30).

New knobs: `BROKER_GPU_DEVICES`, `BROKER_GPU_VERIFY_SECONDS`, and the registry
value `gpu.verify: declared`. See [gpu-placement.md](../gpu-placement.md).

### If you run the frozen v0.5.0 test suite against this branch

Three tests intentionally fail because they assert the **old** cache-aware β — all
three are the ADR 0018 default change, and are documented behavior changes, not
regressions:

- `test_default_beta_is_the_retuned_value`
- `test_cache_pressure_default_beta_substantially_lifts_hit_rate`
- `test_config::test_cache_aware_defaults_preserve_legacy`

Everything else in the v0.5.0 suite passes unchanged (including
`test_pgr_not_clamped`).

## Adopting the new features (all opt-in)

| Want to… | Turn on | Guide |
|---|---|---|
| Rerank / RAG | `supports_rerank: true` on a reranker model | [embeddings-and-rerank](embeddings-and-rerank.md) |
| Semantic cache | `BROKER_SEMANTIC_CACHE_ENABLED=true` | [response-cache](response-cache.md) |
| Web UI | `BROKER_UI_ENABLED=true` | [web-ui](web-ui.md) |
| Per-key limits/budgets/tenants | configure a keys file | [virtual-keys](virtual-keys.md) |
| Ejection cap / per-error policy | `BROKER_MAX_EJECTION_PERCENT`, `BROKER_RETRY_ON_*` | [reliability](reliability.md) |
| Multi-replica shared state | `BROKER_REDIS_URL` | [redis-multi-replica](redis-multi-replica.md) |
| Cascade / latency budget / placement | `router.mode`, headers | [auto-router](auto-router.md) |
| Tracing | `BROKER_TRACING_ENABLED=true` | [observability](observability.md) |
| GPU-safe model swapping (telemetry, measured release, sized models) | `BROKER_GPU_TELEMETRY` + `memory_mib` on models | [gpu-memory](gpu-memory.md) |
| Request queueing under overload | `BROKER_QUEUE_ENABLED=true` | [request-queueing](request-queueing.md) |
| Crash recovery / restart reconciliation | `BROKER_RUNTIME_RECONCILE=adopt\|stop` | [restart-and-recovery](restart-and-recovery.md) |

## Verify the upgrade

```bash
curl -s -H "Authorization: Bearer YOUR_API_KEY" http://127.0.0.1:8080/v1/models   # your models unchanged
curl -s -H "Authorization: Bearer YOUR_API_KEY" http://127.0.0.1:8080/status | python3 -m json.tool
```

Expected: identical model list and routing behavior to before — new blocks in
`/status` (e.g. `reliability`, `cache_aware`) report default-off until you opt in.

## Reference & decisions

- Reference: [CHANGELOG](../../CHANGELOG.md) · [configuration](../configuration.md) ·
  [API reference](../api-reference.md)
- ADRs: [0017 reliability v2](../adr/0017-reliability-v2.md) ·
  [0018 cache-aware v2](../adr/0018-cache-aware-v2.md)
