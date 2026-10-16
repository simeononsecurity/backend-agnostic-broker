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
| `alias` | str | _(required)_ | Public model id used by clients (`model` in requests, `id` in `/v1/models`). |
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
| `keep_alive` | str? | _(none)_ | Ollama keep-alive override (per model). |

### `gpu` (GPURequirements)

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `required` | bool | `false` | Fail if a GPU is unavailable. |
| `device_ids` | list[int] | `[]` | GPU ordinals to use (empty = backend default / CPU). |
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
