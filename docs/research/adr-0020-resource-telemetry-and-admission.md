# Research note — ADR 0020 (resource telemetry + measured release) & ADR 0022 (memory-aware admission)

Scope: how to measure GPU / host memory *vendor-neutrally* by shelling out to the
stock driver tools, which device-isolation environment variable each vendor
expects, why a model swap must **wait for memory to actually come back** before
the next load, and how to size models for admission. Fetched web content is
treated as untrusted data; every adopted fact is cited to a primary source
(driver-tool docs, the Linux kernel docs, AMD ROCm docs, Apple/BSD tools).

## Primary sources

1. **nvidia-smi query mode** — NVIDIA Enterprise Support, *Useful nvidia-smi
   Queries*: `nvidia-smi --query-gpu=memory.total,memory.free,memory.used
   --format=csv`. `memory.total` = total installed GPU memory; `memory.free` =
   total free memory; `memory.used` = memory allocated by active contexts.
   <https://enterprise-support.nvidia.com/s/article/Useful-nvidia-smi-Queries-2>
2. **nvidia-smi per-process memory** — NVIDIA driver docs / field reference:
   `nvidia-smi --query-compute-apps=pid,used_memory --format=csv` lists the PIDs
   that hold memory and how much. (Confirmed in the field write-ups under source
   7.)
3. **ROCm device-isolation env vars** — *ROCm environment variables* (AMD ROCm
   docs): `ROCR_VISIBLE_DEVICES`, `HIP_VISIBLE_DEVICES`, `GPU_DEVICE_ORDINAL`,
   `CUDA_VISIBLE_DEVICES`. **Recommendation (verbatim): "On Linux, use
   `ROCR_VISIBLE_DEVICES`. On Windows, use `HIP_VISIBLE_DEVICES`. For portability
   across different vendors, use `CUDA_VISIBLE_DEVICES`."**
   <https://rocm.docs.amd.com/en/latest/reference/environment-variables/index.html>
4. **amd-smi CLI** — *AMD SMI CLI tool usage* (AMD SMI docs) and *A Deep Dive
   into AMD SMI* (ROCm blog): `amd-smi metric --mem-usage --json` (memory usage
   per block); `amd-smi static --json | jq '.[0]["vram"]'` →
   `{"type":"HBM","size":{"value":196592,"unit":"MB"}}`; the default process
   table lists GPU ID, PID, VRAM memory per process. `--json` / `--csv`
   modifiers on every command.
   <https://rocm.docs.amd.com/projects/amdsmi/en/latest/how-to/amdsmi-cli-tool.html>
   <https://rocm.blogs.amd.com/software-tools-optimization/amd-smi-overview/README.html>
5. **rocm-smi (fallback)** — *Using ROCm SMI* (ROCm SMI lib docs):
   `rocm-smi --showmeminfo vram --json` returns bytes used and total per block
   (`vram`, `vis_vram`, `gtt`); `--showpids` / `--showpidgpus` enumerate KFD PIDs
   and the GPUs they use. `--json` prints machine-readable output.
   <https://rocm.docs.amd.com/projects/rocm_smi_lib/en/latest/how-to/use-python.html>
6. **Linux host memory** — *The /proc Filesystem* (Linux kernel docs):
   `/proc/meminfo` exposes `MemTotal`, `MemFree`, and **`MemAvailable`** — "an
   estimate of how much memory is available for starting new applications,
   without swapping" (the single most useful "am I out of memory?" field; values
   in kB). <https://www.kernel.org/doc/html/latest/filesystems/proc.html>
7. **macOS host memory** — `sysctl -n hw.memsize` returns total physical RAM in
   bytes; `sysctl -n hw.pagesize` returns the page size (16384 on Apple Silicon,
   4096 on Intel); `vm_stat` reports page counts (`Pages free`, `Pages inactive`,
   `Pages speculative`, `Pages wired down`, `Pages occupied by compressor`).
   Available ≈ `(free − speculative + inactive) × pagesize`.
   <https://deepwiki.com/pierotofy/vmem/7.3-macos-system-commands>
8. **Lazy GPU memory release (the reason for *measured release*)** — Netdata,
   *NVIDIA GPU memory leak*: "When a process dies with SIGKILL, the driver
   reclaims its CUDA context lazily, so freed memory can lag by seconds. And if
   the process is a zombie whose context was never destroyed, the memory is held
   indefinitely." NVIDIA's own `jetson-memory-audit` skill: "After you stop a
   vLLM, sglang, or Ollama server … the memory shown as free … may not recover —
   even though the process is gone." fast.ai GPU notes corroborate zombie
   processes that keep VRAM and even disappear from `nvidia-smi`.
   <https://www.netdata.cloud/guides/nvidia-gpu/nvidia-gpu-memory-leak/>
   <https://github.com/NVIDIA/skills/blob/HEAD/skills/jetson-memory-audit/SKILL.md>

## Adopt / reject

| Decision | Choice | Rationale (sourced) |
|---|---|---|
| Measurement mechanism | **Shell out to the stock driver CLI** (`nvidia-smi`, `amd-smi`/`rocm-smi`, `vm_stat`/`sysctl`, read `/proc/meminfo`), argv-only, absolute/allow-listed path, timeout, parse defensively | One uniform, vendor-neutral provider interface; no compiled bindings; a missing tool degrades to "unavailable" instead of a crash. Each tool's output is documented (1,3–7). |
| NVML / `pynvml` | **Reject** | NVML would be NVIDIA-only and a build/runtime dependency, breaking the "no path assumes NVIDIA" + "default OFF, zero new deps" constraints. `nvidia-smi --query-gpu`/`--query-compute-apps` give total/used/free and per-PID memory with no library (1,2). Documented as a rejected alternative in ADR 0020. |
| `psutil` for host memory | **Reject** | `/proc/meminfo` (Linux, `MemAvailable`, source 6) and `vm_stat`+`sysctl` (macOS, source 7) are stable, dependency-free, and already what psutil reads underneath. Keeps the `host` provider zero-dependency. |
| AMD device env var | **`ROCR_VISIBLE_DEVICES` on Linux** (set it for the `amd` provider); keep `CUDA_VISIBLE_DEVICES` for `nvidia`/default; expose `HIP_VISIBLE_DEVICES` as a documented override | Verbatim ROCm recommendation (3). The broker targets Linux/containers; ROCR is the runtime-level isolation var honored by HIP/ROCr. `CUDA_VISIBLE_DEVICES` stays the portable default so existing configs are unchanged. |
| nvidia-smi output format | `--format=csv,noheader,nounits` → integer MiB | Stable, locale-independent, trivially parsed; avoids the "MiB" suffix and header row (1). |
| Host "free" definition | Linux: `MemAvailable`; macOS: `(free − speculative + inactive) × pagesize` | `MemAvailable` is the kernel's own honest estimate (6); the macOS formula mirrors how Activity Monitor / psutil compute "available" (7). Labeled `measured` and documented as *available*, not raw free. |
| Measured release | **Wait for (a) the process group to be gone AND (b) with telemetry on, the unloaded PID no longer on the device and free memory non-decreasing**, bounded by a timeout; else `release_stuck` + 503 | Driver reclaims CUDA contexts lazily after SIGKILL (8); starting the next load immediately races an OOM. We never force a GPU reset or kill a foreign PID — we wait and surface the stuck holder. |
| Ollama / external daemons | Memory only as **reported by the daemon** (`/api/ps` `size_vram`), labeled `reported`, never mixed with `measured` | The broker does not own those processes and must not attribute host/GPU measurements to them; honest-numbers constraint. |
| Admission budget (ADR 0022) | Telemetry on → measured free − headroom; telemetry off → declared `BROKER_GPU_MEMORY_BUDGET_MIB` − Σ declared `memory_mib` of resident models on the device | Measured when we can, declared bookkeeping when we can't; the two are never conflated and are labeled distinctly in `/status`. |
| Capacity-unavailable signal | A typed `CapacityUnavailableError(ConcurrencyLimitError)` carrying `reason` + `holder` (429) | Gives the ADR 0023 queue a clean predicate to wait on instead of a bare 429 string. |

## Host-provider smoke (this Mac, `BROKER_GPU_TELEMETRY=host`)

Recorded from the real run on the development machine (macOS, Apple Silicon),
reading `sysctl hw.memsize`/`hw.pagesize` + `vm_stat` (no GPU present), so the
`source` is `measured` and the single device is the host unified-memory pool:

```
$ PYTHONPATH=. .venv/bin/python -m broker.resources_probe host
{
  "provider": "host",
  "status": "ok",
  "source": "measured",
  "reason": null,
  "captured_at": 1792541084.409,
  "devices": [
    {
      "index": "host",
      "name": "host",
      "total_mib": 24576,
      "used_mib": 20428,
      "free_mib": 4148,
      "processes": []
    }
  ]
}
exit=0
```

`total_mib` 24576 = the machine's 24 GiB of RAM; `free_mib` is derived from
`(Pages free + inactive + speculative) × pagesize` (the available-memory basis,
source 7). The probe exits 0 on `status: ok`, 2 on `unavailable` — so a smoke
script distinguishes "no measurement" from "crashed". The `host` provider needs
no GPU and no extra dependency, which is why it is the one we can exercise on CI
and on this laptop.
