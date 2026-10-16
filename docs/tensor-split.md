# Tensor Split

## Confirmed broker behavior

- Registry field `gpu.tensor_split` is a `list[float]` (e.g. `[0.5, 0.5]`).
- For broker-managed runtimes, `httpbase._build_startup_argv` exposes it to the
  `startup_command` template as the `{tensor_split}` placeholder, joined as a
  comma-separated string (e.g. `0.5,0.5`). It is passed only if set.
- Consumed primarily by **llama.cpp** (and **Strata**, deployment-specific). For
  **vLLM**, use `gpu.tensor_parallel_size` instead (see `docs/backends/vllm.md`).
- `GET /status` surfaces the configured `tensor_split` per model.

## Example (llama.cpp)

```yaml
gpu:
  device_ids: [0, 1]
  tensor_split: [0.6, 0.4]
startup_command:
  - /usr/local/bin/llama-server
  - --model
  - "{model_path}"
  - --tensor-split
  - "{tensor_split}"      # broker substitutes "0.6,0.4"
```

## Runtime reference (verify before production)

- llama.cpp `--tensor-split` splits model tensors across GPUs by the given
  proportions. Confirm the exact format/semantics for your `llama-server` build.
- vLLM uses `--tensor-parallel-size` (an integer degree), not a fractional
  split.

Do not add a runtime flag the broker does not template; extend the registry
`startup_command` with the verified flag for your runtime version.
