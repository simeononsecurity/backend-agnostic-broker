# Backend: vLLM

## Broker-confirmed behavior (authoritative — from `broker/adapters/vllm.py` + `httpbase.py`)

- **Broker-managed lifecycle.** vLLM's OpenAI-compatible server loads **one
  model at process startup** and has no native per-request lazy loading, so the
  broker starts the process on the first selecting request and stops it after
  the idle timeout.
- **No-shell start.** `start()` builds an argv **list** from the registry
  `startup_command` template via `security.build_argv`, after
  `validate_executable` (allow-listed dir) and `validate_model_path`
  (allow-listed root). GPU device selection is passed as `CUDA_VISIBLE_DEVICES`
  in the child environment from `gpu.device_ids` (or the global
  `BROKER_CUDA_VISIBLE_DEVICES`).
- **Templated placeholders available:** `{exe}`, `{model_path}`,
  `{runtime_model_id}`, `{context_limit}`, `{output_limit}`,
  `{tensor_parallel_size}`, `{pipeline_parallel_size}`, `{gpu_memory_fraction}`,
  `{tensor_split}`, `{layer_split}` (only those set on the entry are provided).
- **Readiness / health.** HTTP probe (default `GET /health`, expect `200`) until
  ready or timeout; if the child exits during startup the broker raises
  `ModelLoadError` with recent logs.
- **Chat / streaming.** Forwarded to `{base_url}/v1/chat/completions`; upstream
  `model` pinned to `runtime_model_id`.
- **Unload / idle.** `unload` stops the process; the idle sweep unloads after
  `idle_timeout_seconds`.

## Registry example (verified template)

```yaml
- alias: mistral-vllm
  backend: vllm
  runtime_model_id: mistralai/Mistral-7B-Instruct-v0.3
  model_path: /models/Mistral-7B-Instruct-v0.3
  base_url: http://127.0.0.1:8000
  context_limit: 32768
  startup_command:
    - /usr/local/bin/vllm
    - serve
    - "{model_path}"
    - --host
    - 127.0.0.1
    - --port
    - "8000"
    - --max-model-len
    - "{context_limit}"
    - --tensor-parallel-size
    - "{tensor_parallel_size}"
    - --gpu-memory-utilization
    - "{gpu_memory_fraction}"
  readiness: { kind: http, path: /health, expect_status: 200, timeout_seconds: 300 }
  gpu: { required: true, device_ids: [0], tensor_parallel_size: 1, gpu_memory_fraction: 0.9 }
```

## Runtime reference (vendor docs — verify before production)

Confirm flags against your installed vLLM version:

- Server: `vllm serve <model>` (or `python -m vllm.entrypoints.openai.api_server`),
  OpenAI-compatible routes, `/health` for readiness.
- Tensor parallelism: `--tensor-parallel-size`; pipeline parallelism:
  `--pipeline-parallel-size`; VRAM budget: `--gpu-memory-utilization` (0–1);
  context: `--max-model-len`.
- MTP/speculative decoding is version-dependent; see `docs/mtp.md`. Do not
  enable unless your vLLM version documents it.

## Known limitations

- One model per process; concurrent distinct models each consume a process and
  GPU memory (bounded by `BROKER_MAX_LOADED_MODELS`).
- First-request startup can be slow (weights load); set `startup_timeout_seconds`
  / `readiness.timeout_seconds` generously.

## Evidence sources

- `broker/adapters/vllm.py`, `broker/adapters/httpbase.py`, `registry.example.yaml`.
- Official vLLM docs: https://docs.vllm.ai/
