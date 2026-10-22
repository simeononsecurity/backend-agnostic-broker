# Model Registry Reference

The registry is a declarative YAML (or JSON) file listing every model the broker
can serve. It is the **only** source of truth for what may be loaded — a request
selects a model by its public `alias` and can never introduce a new backend,
executable, or path. Entries are validated by pydantic at load time
(`broker/registry.py`), so misconfiguration fails fast. Unknown fields are
rejected (`extra: forbid`). See `registry.example.yaml` for a full example.

Top-level shape:

```yaml
models:
  - alias: echo-fake
    backend: fake
    runtime_model_id: echo
```

`BROKER_ENABLED_BACKENDS` filters the loaded registry, so you can ship one file
and toggle backends via config. Duplicate aliases are rejected.

## `ModelEntry` fields

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `alias` | str | _(required)_ | Unique member id. Also the public model id **unless** `pool` is set. |
| `pool` | str? | _(none)_ | Load-balancing pool name. Entries sharing a `pool` form one logical model clients call by the pool name; requests route across members with failover. See [routing-and-health.md](routing-and-health.md). |
| `backend` | enum | _(required)_ | One of `fake`, `ollama`, `vllm`, `llamacpp`, `strata`. |
| `runtime_model_id` | str | _(required)_ | The model id the backend runtime uses. |
| `model_path` | str? | _(none)_ | Local model path (required by file-based runtimes); validated against `BROKER_ALLOWED_MODEL_ROOTS`. |
| `context_limit` | int | `8192` | Advertised context window. |
| `output_limit` | int | `2048` | Advertised max output tokens. |
| `gpu` | object | _(see below)_ | GPU placement controls. |
| `mtp` | object | _(see below)_ | Multi-token-prediction / speculative controls. |
| `base_url` | str? | _(none)_ | Where the runtime serves (HTTP backends); added to the SSRF allow-list. |
| `startup_command` | list[str] | `[]` | argv **template** for broker-managed runtimes (tokens + `{placeholder}`s). |
| `readiness` | object | _(see below)_ | Readiness probe. |
| `unload_command` | list[str] | `[]` | Optional argv template for unload. |
| `idle_timeout_seconds` | float | `300` | Per-model idle unload timeout (overrides global). |
| `request_timeout_seconds` | float | `300` | Per-model request timeout. |
| `startup_timeout_seconds` | float | `120` | Per-model readiness timeout. |
| `supports_streaming` | bool | `true` | Advertised streaming capability. |
| `supports_tools` | bool | `false` | Advertised tool-call capability. |
| `supports_vision` | bool | `false` | Advertised vision capability. |
| `supports_rerank` | bool | `false` | Can serve `POST /v1/rerank` (ADR 0013). A cross-encoder/reranker runtime (vLLM or llama.cpp) sets this true; requesting rerank on a model without it returns 400. Surfaced in `/v1/models` capabilities. |
| `keep_alive` | str? | _(none)_ | Ollama keep-alive override (per model). |
| `tags` | list[str] | `[]` | Auto-router class tags, e.g. `code`, `vision`, `reasoning`, `long-context`, `tools`, `json`. Main control surface for `auto`. |
| `description` | str? | _(none)_ | One-line description shown in the router's candidate table. |
| `router_eligible` | bool | `true` | Whether the `auto` router may select this model. |
| `priority` | int | `0` | Auto-router tie-break preference (higher = preferred). |
| `cost_tier` | int | `2` | Relative cost/size `1`(cheapest)..`5`(most expensive); the router prefers the cheapest capable model. |
| `placement` | list[str] | `[]` | Data-residency labels (e.g. `local`, `trusted`, `region:eu`). A request carrying a placement requirement is only served by models/pool members that carry **all** required labels; otherwise `403 placement_denied` (fail-closed). See [auto-router.md](auto-router.md). |
| `memory_mib` | int? | _(none)_ | Declared resident footprint in MiB for memory-aware admission (ADR 0022, opt-in). When set, a load is admitted only if it fits the device budget — measured free memory when `BROKER_GPU_TELEMETRY` is on, else the declared `BROKER_GPU_MEMORY_BUDGET_MIB` — evicting idle models on the same device(s) first, else `429 capacity_unavailable`. The device(s) come from `gpu.device_ids` (else a shared `default` bucket). Unset ⇒ v0.6.0 count-only behavior. See [gpu-memory.md](guides/gpu-memory.md). |

### `router` (top-level, optional)

Data-driven policy for the `auto` alias (see [auto-router.md](auto-router.md)):

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `default` | str? | _(none)_ | Deterministic fallback model id when the classifier returns an invalid choice. |
| `policy` | str? | _(none)_ | System-prompt / routing-policy override for the LLM classifier. |
| `overrides` | list | `[]` | Hard `when → use` rules, first match bypasses the LLM (skipped if the target isn't a valid candidate). |
| `weights` | object | _(see below)_ | Heuristic scoring weights: `capability` (3.0), `cost` (1.0), `prefer_loaded` (1.0), `priority` (1.0), `cost_normalized` (true). `cost_normalized: true` (default since v0.6.0) normalizes the cost term to `[0,1]·cost` so a single capability match always beats price — required so a warm cheap model doesn't receive code/math/reasoning prompts; `false` restores v0.5.0's un-normalized `(5 − cost_tier)·cost`. See auto-router.md "Warm-state evaluation". |
| `mode` | str | `single` | `single` (one-shot, default) or `cascade` (cheapest-capable first, escalate once on a signal — ADR 0011). |
| `cascade` | object | _(see below)_ | Escalation signals for `mode: cascade`. |
| `latency_budget_ms` | float? | _(none)_ | Default latency budget (ms); the `x-broker-latency-budget-ms` header wins, then this, then `BROKER_ROUTER_LATENCY_BUDGET_MS`. |

Each override `when` supports `has_images`, `has_tools`, `needs_json`,
`min_ctx_tokens`, and `regex` (matched against the last user message).

#### `cascade` (CascadeConfig)

Signals checked against the cheapest-capable model's actual answer; if any fires,
the request escalates once to a stronger capable model (ADR 0011).

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `escalate_on_length` | bool | `true` | Escalate when the first hop was truncated (`finish_reason == "length"`). |
| `escalate_on_empty` | bool | `true` | Escalate on an empty / too-short answer. |
| `min_chars` | int | `1` | Minimum acceptable answer length (characters, stripped). |
| `escalate_on_refusal` | bool | `true` | Escalate when the answer matches a refusal regex. |
| `refusal_patterns` | list[str] | _(built-in set)_ | Refusal regexes (validated at load). |
| `stream_mode` | str | `fallback_single` | Streaming cascade mode (ADR 0019): `fallback_single` (= v0.5.0, serve the single strong pick) or `buffer` (chat endpoint buffers the cheap hop, scores it, then replays it or streams the stronger hop — never mixing two models). |
| `scorer` | object | _(disabled)_ | Opt-in calibrated reliability scorer (ADR 0019); see below. |

#### `cascade.scorer` (ScorerConfig) — ADR 0019

Opt-in. When `enabled`, the cheap hop's answer is reduced to one reliability score
in `[0, 1]` and the hop escalates when `score < threshold`, instead of the booleans
above. The score is a weighted mean of **graded** features (length adequacy +
optional token-logprob confidence) with **hard gates** (refusal / truncation cap the
score to 0). Default-off preserves v0.5.0 behavior.

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `enabled` | bool | `false` | Use the scorer instead of the boolean signals. |
| `threshold` | float | `0.5` | Escalate when reliability `< threshold` (must be in `[0,1]`). |
| `target_chars` | int | `200` | Answer length (stripped) at which the length feature saturates to 1. |
| `weight_length` | float | `1.0` | Weight of the length-adequacy feature (`>= 0`). |
| `weight_logprob` | float | `0.0` | Weight of the token-logprob confidence feature, used only when the response carries logprobs (`>= 0`). |
| `gate_refusal` | bool | `true` | A refusal match caps the score to 0 (always escalate). |
| `gate_truncation` | bool | `true` | `finish_reason == "length"` caps the score to 0 (always escalate). |

### `pricing` (top-level, optional) — ADR 0008

Per-model prices used to charge per-key **cost budgets** (see
[configuration.md](configuration.md) and ADR 0008). Keyed by the **public** model
name (a pool name, or a standalone alias). Prices are **USD per million tokens**.
Models without an entry are treated as free, so cost budgets simply don't apply to
them (token budgets need no pricing).

```yaml
pricing:
  chat:                     # a pool or alias name
    input_per_million: 0.20
    output_per_million: 0.60
```

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `input_per_million` | float | `0.0` | USD per 1,000,000 prompt (input) tokens. |
| `output_per_million` | float | `0.0` | USD per 1,000,000 completion (output) tokens. |

A request costs `prompt·input + completion·output`; the broker accumulates spend
in integer micro-USD so counters stay exact.

### `gpu` (GPURequirements)

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `required` | bool | `false` | Require a GPU. **Enforced at load (ADR 0025):** an empty device inventory, or a `device_ids` entry not in it, fails the load with **503** `gpu_unavailable` before any spawn/eviction (no CPU fallback); placement is verified after load where the backend allows it (managed runtimes via telemetry, Ollama via `/api/ps` `size_vram`). A model **without** `required` is unchanged. See [gpu-placement.md](gpu-placement.md). |
| `device_ids` | list[int] | `[]` | GPU ordinals to use (empty = backend default / CPU). When `required`, each id must be in the device inventory (measured telemetry devices, else `BROKER_GPU_DEVICES` / `BROKER_CUDA_VISIBLE_DEVICES`). |
| `verify` | str? | _(none)_ | Only value: `declared`. Required to set `required: true` on an **external** OpenAI-compatible endpoint (no `startup_command`, non-Ollama) the broker cannot verify — otherwise that is a startup config error. Asserts GPU placement out-of-band; shown as `declared` in `/status`. |
| `gpu_memory_fraction` | float? | _(none)_ | Per-GPU VRAM fraction `(0,1]` (e.g. vLLM). |
| `tensor_split` | list[float] | `[]` | Fractional tensor split across GPUs (llama.cpp / strata). |
| `layer_split` | str? | _(none)_ | Layers to offload to GPU (llama.cpp `n-gpu-layers`), e.g. `"all"` or a number. |
| `tensor_parallel_size` | int? | _(none)_ | Tensor-parallel degree (vLLM). |
| `pipeline_parallel_size` | int? | _(none)_ | Pipeline-parallel degree (vLLM). |

### `mtp` (MTPConfig)

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `enabled` | bool | `false` | Enable speculative / multi-token prediction (backend-dependent). |
| `num_speculative_tokens` | int? | _(none)_ | Speculative token count. |
| `draft_model` | str? | _(none)_ | Draft model id for speculative decoding. |

> These fields are backend-dependent. See `docs/mtp.md` — only enable where the
> target runtime confirms support.

### `readiness` (ReadinessProbe)

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `kind` | str | `http` | `http` (poll `path`) or `log` (wait for `log_marker`). |
| `path` | str | `/health` | HTTP path to poll on `base_url`. |
| `expect_status` | int | `200` | Expected HTTP status for readiness. |
| `log_marker` | str? | _(none)_ | Log substring signalling readiness (for `kind: log`). |
| `interval_seconds` | float | `0.5` | Poll interval. |
| `timeout_seconds` | float | `120` | Probe timeout. |

## argv templates and placeholders

`startup_command`/`unload_command` are argv **lists**, never shell strings.
Whole-token `{name}` placeholders are substituted from a whitelisted set by
`broker/security.py::build_argv`; unknown placeholders and shell metacharacters
are rejected. This is how GPU/tensor/layer settings reach a runtime without ever
exposing a shell or letting a request inject flags.

## Worked example

```yaml
models:
  - alias: llama3-8b-gpu
    backend: llamacpp
    runtime_model_id: llama3-8b
    model_path: /models/llama3-8b/ggml-model-q4_k_m.gguf
    base_url: http://127.0.0.1:9001
    context_limit: 8192
    output_limit: 2048
    gpu:
      required: true
      device_ids: [0]
      layer_split: "all"
      tensor_split: [1.0]
    readiness:
      kind: http
      path: /health
      expect_status: 200
      timeout_seconds: 120
    idle_timeout_seconds: 600
    supports_streaming: true
```
