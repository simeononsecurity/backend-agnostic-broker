# How-to: GPU / host memory telemetry, measured release, and sized models

> Opt-in and **OFF by default** — with nothing configured the broker behaves
> exactly like v0.6.0. This guide covers ADR 0020 (resource telemetry + measured
> release) and ADR 0022 (memory-aware admission).

## When to enable

Turn this on when the broker **spawns runtimes** (vLLM / llama.cpp / Strata) and
swaps models under memory pressure, and you want to:

- stop a model swap from OOMing because the previous runtime's VRAM hadn't come
  back yet (**measured release**);
- admit a model only when it actually fits the GPU (**sized models**);
- see measured per-device memory and which PID holds it in `/status`.

If the broker only proxies an external daemon (Ollama), telemetry still reports the
daemon's self-reported memory (labeled `reported`), but there is no broker-owned
process to measure or release.

## 1. Enable telemetry

Pick the provider for your hardware:

```bash
BROKER_GPU_TELEMETRY=nvidia   # nvidia-smi query mode
BROKER_GPU_TELEMETRY=amd      # amd-smi / rocm-smi JSON
BROKER_GPU_TELEMETRY=host     # system RAM: CPU or Apple-silicon unified memory
BROKER_GPU_TELEMETRY=auto     # probe nvidia -> amd -> host, first that works
```

Verify from the CLI before wiring it into the broker:

```bash
python -m broker.resources_probe nvidia   # exit 0 = ok, 2 = unavailable
python -m broker.resources_probe host
```

Each provider shells out **argv-only** to an **absolute, allow-listed** binary
(override with `BROKER_GPU_TELEMETRY_NVIDIA_SMI_PATH` etc. — the path must be
absolute). A missing tool or garbled output degrades to `status: unavailable` with
a reason; it never crashes the broker and never invents a number. Samples are
cached for `BROKER_GPU_TELEMETRY_POLL_SECONDS` (default 5 s).

Read it back in `/status` under `resources`:

```json
"resources": {
  "telemetry": {"provider": "nvidia", "enabled": true, "poll_seconds": 5.0},
  "source": "measured",
  "devices": [{"index": "0", "name": "NVIDIA A100-SXM4-40GB",
               "total_mib": 40960, "free_mib": 39000, "used_mib": 1960,
               "processes": [{"pid": 12345, "mib": 1900}]}],
  "release": {"timeout_seconds": 30.0},
  "release_stuck": []
}
```

Measured numbers carry `source: measured`. Ollama daemon memory appears separately
under `resources.reported` (`source: reported`) and is **never mixed** with
measured devices.

### Which device env var?

The broker sets the device-isolation env var the active provider expects when it
spawns a runtime: `ROCR_VISIBLE_DEVICES` for `amd` (the ROCm runtime-level
isolation var on Linux), `CUDA_VISIBLE_DEVICES` otherwise (the portable default,
unchanged from v0.6.0). Override with `BROKER_GPU_DEVICE_ENV_VAR=HIP_VISIBLE_DEVICES`
if your runtime requires it. The devices come from the model's `gpu.device_ids`.

## 2. Measured release (safe swaps)

With telemetry on, after the broker evicts a model to load another on the same
device, the next load **waits** until the evicted process group is gone **and** the
device no longer reports its PID — bounded by `BROKER_RELEASE_TIMEOUT_SECONDS`
(default 30 s). This closes the race where a driver reclaims VRAM lazily after
SIGKILL (or a zombie never releases it) and the next load OOMs.

If the memory never comes back in time, the model enters a visible **`release_stuck`**
state and the waiting load fails **503** naming the stuck PID/device/MiB, instead of
starting into an OOM:

```
503 release_stuck: runtime for 'mistral-vllm' did not release device 0 within 30s
```

The broker **never resets the GPU and never kills a process it did not start** — it
surfaces the holder so you can investigate (`nvidia-smi`, `kill` the zombie, etc.).

With telemetry **off**, swap timing is identical to v0.6.0 — the only change is that
a process still alive after SIGKILL is now logged and shown as `survived_kill` in
`/status` instead of being silently ignored.

## 3. Size your models (memory-aware admission)

Declare a model's footprint in the registry:

```yaml
  - alias: mistral-vllm
    backend: vllm
    # ...
    memory_mib: 18000          # declared resident MiB
    gpu:
      device_ids: [0]
```

Then give the broker a budget:

- **Telemetry on** → the budget is **measured free memory** minus
  `BROKER_GPU_MEMORY_HEADROOM_MIB` (default 1024) on the model's device(s).
- **Telemetry off** → set a **declared** budget:

  ```bash
  BROKER_GPU_MEMORY_BUDGET_MIB=40000          # same budget for every device
  BROKER_GPU_MEMORY_BUDGET_MIB=0=24000,1=48000 # per-device map (key `default` = unpinned)
  ```

On load, if the model does not fit, the broker evicts **LRU idle models on the same
device(s)** (each followed by the measured-release wait) until it fits; if it still
does not fit it returns **429 `capacity_unavailable`** with a `reason` and a
`holder`:

```json
{"error": {"type": "capacity_unavailable", "reason": "measured_insufficient",
           "holder": "device 0", "message": "not enough memory to load 'mistral-vllm': ..."}}
```

Count-based `BROKER_MAX_LOADED_MODELS` still applies as an additional limit. Models
**without** `memory_mib` keep v0.6.0 behavior. Recent admission decisions
(`fits` / `evicted X` / `blocked`) are shown in `/status` under
`resources.admission.recent`.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `resources.telemetry.enabled` is false | `BROKER_GPU_TELEMETRY` is `off` (default) or an invalid value. |
| `sample.status: unavailable` | The driver tool isn't on the allow-list/absolute path, timed out, or returned no devices. Run `python -m broker.resources_probe <provider>` and read the `reason`. |
| A model is stuck in `release_stuck` | A previous runtime's VRAM never came back (lazy reclaim or a zombie). Inspect the PID/device in `resources.release_stuck`, free it, then retry; or raise `BROKER_RELEASE_TIMEOUT_SECONDS`. |
| `429 capacity_unavailable` on every load | The device budget is too small for the model, or the holder can't be evicted (it's serving). Raise the budget/headroom, pin to another device, or wait for the holder to go idle. |
| Sized model never engages admission | Neither telemetry nor `BROKER_GPU_MEMORY_BUDGET_MIB` is set — the broker can't account memory, so it keeps count-only behavior. |

See also: [configuration.md](../configuration.md), [model-registry.md](../model-registry.md),
ADR 0020, ADR 0022, and the tested [`examples/gpu-memory/`](../../examples/gpu-memory/).
