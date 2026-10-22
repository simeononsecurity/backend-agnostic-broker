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
- **`gpu.required` is enforced, not advisory (ADR 0025).** Enforcement runs in the
  shared admission path for any entry that sets `gpu.required` — a model without
  it is unchanged. There is **no CPU fallback**.
  - **Device inventory.** The devices the broker may place on are the telemetry
    provider's measured devices when `BROKER_GPU_TELEMETRY` is on, else a declared
    list from `BROKER_GPU_DEVICES` (ordinals), falling back to
    `BROKER_CUDA_VISIBLE_DEVICES`. The source is labelled `measured` / `declared`
    / `none` in `/status`.
  - **Pre-load gate (all backends), before any spawn or eviction.** A load fails
    fast with **503** `gpu_unavailable` when `gpu.required` is set and the
    inventory is empty, or when a `gpu.device_ids` entry is not in the inventory.
    Nothing is spawned or evicted; within a pool the request fails over to a
    member that qualifies, but the failing member is not sleep-retried.
  - **Post-load verification, per backend:**
    - **Broker-managed runtimes** (vLLM / llama.cpp / Strata with a
      `startup_command`), evidence `measured`: with telemetry on, the runtime's
      process group must hold memory on a required device within
      `BROKER_GPU_VERIFY_SECONDS` (default 30). Otherwise the model is unloaded
      and the request fails **503** `gpu_placement_failed`. Telemetry off → honest
      "no measurement" (shown as `unverified`); the pre-load declared-inventory
      gate still applies.
    - **Ollama**, evidence `reported`: after load, `/api/ps` must report
      `size_vram > 0` for the model. Otherwise it is unloaded and the request
      fails **503** `gpu_placement_failed`; the evidence is labelled `reported`.
    - **External OpenAI-compatible endpoints** (no `startup_command`, non-Ollama),
      evidence `external`: the broker owns no lifecycle and cannot verify
      placement, so `gpu.required: true` there is a **startup configuration error**
      unless the entry also sets `gpu.verify: declared`, an explicit operator
      statement shown as `declared` in `/status`.
- **Status visibility**: `GET /status` includes each model's GPU allocation
  (`device_ids`, `tensor_split`, `layer_split`, `tensor_parallel_size`) via the
  adapter `collect_status`, plus a per-model `gpu` block with `required`, the
  inventory source, the evidence mode, and the last verification result (ADR 0025).

## Registry fields

| Field | Used by | Meaning |
|-------|---------|---------|
| `gpu.required` | all | Require a GPU. Enforced at load (ADR 0025): empty inventory or an unknown `device_ids` → 503 `gpu_unavailable`; placement is verified after load where the backend allows it. |
| `gpu.device_ids` | broker-managed | Sets `CUDA_VISIBLE_DEVICES` for the child; each id must be in the device inventory when `gpu.required`. |
| `gpu.verify` | external + `required` | Only value: `declared`. Required to set `gpu.required` on an external OpenAI-compatible endpoint the broker cannot verify; asserts placement out-of-band and shows as `declared` in `/status`. **Rejected (startup `config_error`) on any backend the broker CAN verify** — a broker-managed runtime (`measured`) or Ollama (`reported`) — so it can never silently disable enforcement. |
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
