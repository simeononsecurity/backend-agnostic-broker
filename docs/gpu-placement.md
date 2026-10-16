# GPU Placement

GPU behavior is controlled globally by config and per-model by the registry.

## Confirmed broker behavior

- **Visibility** (`broker/config.py`): `BROKER_GPU_VISIBLE` (default `false`) and
  `BROKER_CUDA_VISIBLE_DEVICES` (e.g. `0,1`) describe the host/runtime GPU
  exposure. In Compose, GPU passthrough is only enabled by the GPU override
  (`docker-compose.gpu.yml`), which also sets these.
- **Per-model device selection** (`registry.py` `GPURequirements`): `gpu.required`
  (fail if unavailable), `gpu.device_ids` (ordinals). For broker-managed
  runtimes, `broker/adapters/httpbase.py::_process_env` sets the child process
  `CUDA_VISIBLE_DEVICES` from `gpu.device_ids`, falling back to the global
  `BROKER_CUDA_VISIBLE_DEVICES`.
- **Status visibility**: `GET /status` includes each model's GPU allocation
  (`device_ids`, `tensor_split`, `layer_split`, `tensor_parallel_size`) via the
  adapter `collect_status`.

## Registry fields

| Field | Used by | Meaning |
|-------|---------|---------|
| `gpu.required` | all | Require a GPU. |
| `gpu.device_ids` | broker-managed | Sets `CUDA_VISIBLE_DEVICES` for the child. |
| `gpu.gpu_memory_fraction` | vLLM (`--gpu-memory-utilization`) | VRAM budget `(0,1]`. |
| `gpu.tensor_split` | llama.cpp / strata | See `docs/tensor-split.md`. |
| `gpu.layer_split` | llama.cpp | See `docs/layer-split.md`. |
| `gpu.tensor_parallel_size` | vLLM | Tensor-parallel degree. |
| `gpu.pipeline_parallel_size` | vLLM | Pipeline-parallel degree. |

These reach a runtime only through the model's `startup_command` argv template
(no shell, no request-supplied flags). A field left unset is simply not passed.

## Compose GPU passthrough

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
```

Requires the NVIDIA Container Toolkit; the override reserves NVIDIA devices and
raises `shm_size`. The base file remains CPU-only for CI.

> Runtime flag names (e.g. vLLM `--gpu-memory-utilization`) are vendor-defined —
> verify against your installed runtime version.
