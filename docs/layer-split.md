# Layer Split

## Confirmed broker behavior

- Registry field `gpu.layer_split` is a string (e.g. `"35"` or `"all"`).
- For broker-managed runtimes, it is exposed to the `startup_command` template as
  the `{layer_split}` placeholder (passed only if set).
- Consumed by **llama.cpp** as the GPU-offload layer count
  (`--n-gpu-layers`/`-ngl`). `GET /status` surfaces it per model.

## Example (llama.cpp)

```yaml
gpu:
  device_ids: [0]
  layer_split: "35"     # offload 35 layers to the GPU
startup_command:
  - /usr/local/bin/llama-server
  - --model
  - "{model_path}"
  - --n-gpu-layers
  - "{layer_split}"
```

Use a large number (or your runtime's "all" convention) to offload the whole
model; use fewer layers to fit limited VRAM, leaving the rest on CPU.

## Runtime reference (verify before production)

- llama.cpp `--n-gpu-layers`/`-ngl` controls how many transformer layers are
  offloaded to the GPU. Confirm the "offload all" convention for your build
  (often a sufficiently large integer).
- vLLM does not use a layer-split flag; it loads the full model on its assigned
  GPUs (see `docs/backends/vllm.md`).
