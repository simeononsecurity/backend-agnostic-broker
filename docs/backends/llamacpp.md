# Backend: llama.cpp

## Broker-confirmed behavior (authoritative — from `broker/adapters/llamacpp.py` + `httpbase.py`)

- **Broker-managed lifecycle.** `llama-server` loads a single GGUF model at
  process startup with no native lazy loading, so the broker starts it on first
  request and stops it after the idle timeout.
- **No-shell start.** argv **list** from the registry `startup_command` template
  (`security.build_argv`), after `validate_executable` and `validate_model_path`.
  GPU devices are passed via `CUDA_VISIBLE_DEVICES` from `gpu.device_ids`.
- **Templated placeholders:** `{exe}`, `{model_path}`, `{context_limit}`,
  `{layer_split}` (n-gpu-layers), `{tensor_split}`, etc. (only those set).
- **Readiness / health.** HTTP probe `GET /health` expect `200`.
- **Chat / streaming.** `{base_url}/v1/chat/completions` (OpenAI-compatible),
  `model` pinned to `runtime_model_id`.
- **Unload / idle.** `unload` stops the process; idle sweep enforces
  `idle_timeout_seconds`.

## Registry example (verified template)

```yaml
- alias: qwen-llamacpp
  backend: llamacpp
  runtime_model_id: qwen2.5-7b-instruct
  model_path: /models/qwen2.5-7b-instruct-q4_k_m.gguf
  base_url: http://127.0.0.1:8081
  context_limit: 8192
  startup_command:
    - /usr/local/bin/llama-server
    - --model
    - "{model_path}"
    - --host
    - 127.0.0.1
    - --port
    - "8081"
    - --ctx-size
    - "{context_limit}"
    - --n-gpu-layers
    - "{layer_split}"
  readiness: { kind: http, path: /health, expect_status: 200, timeout_seconds: 120 }
  gpu: { required: true, device_ids: [0], layer_split: "35", tensor_split: [1.0] }
```

## Runtime reference (vendor docs — verify before production)

Confirm flags against your `llama-server` build:

- Server: `llama-server` (llama.cpp), OpenAI-compatible
  `/v1/chat/completions` and `/health`.
- GPU offload: `--n-gpu-layers` / `-ngl` (number of layers, or enough to offload
  all); multi-GPU split: `--tensor-split`; context: `--ctx-size` / `-c`.
- Continuous batching and additional performance flags are build/version
  dependent; see `docs/layer-split.md` and `docs/tensor-split.md`.

## Known limitations

- One model per process; `layer_split`/`tensor_split` effectiveness depends on
  the build's GPU support (CUDA/Metal/etc.).
- Readiness time depends on model size and offload; tune timeouts accordingly.

## Evidence sources

- `broker/adapters/llamacpp.py`, `broker/adapters/httpbase.py`, `registry.example.yaml`.
- Official llama.cpp server docs: https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
