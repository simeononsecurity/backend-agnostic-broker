# ADR 0020: Vendor-neutral resource telemetry + measured release

Status: Accepted
Date: 2026-10-20
Supersedes: —
Related: ADR 0003 (pools/sticky/health), ADR 0022 (memory-aware admission),
ADR 0023 (request queueing), `docs/research/adr-0020-resource-telemetry-and-admission.md`

## Context

The broker swaps models under `max_loaded_models` by evicting the LRU idle model
and **immediately** loading the next one (`ModelManager._maybe_evict`). Teardown
(`ProcessSupervisor.stop`) sends SIGTERM, then SIGKILL after a grace period, waits
≤5 s more, and **returns even if the process is still alive**. Nothing measures GPU
or host memory. Two real failure modes follow:

1. **Lazy / zombie device reclaim.** After SIGKILL a driver reclaims the CUDA/ROCm
   context lazily, so freed VRAM can lag by seconds; a zombie whose context was
   never destroyed holds it indefinitely (research sources 8). Starting the next
   load immediately races an out-of-memory error.
2. **No visibility.** A surviving process or unreleased device is swallowed
   silently — operators cannot see why the next load OOM'd.

We want to measure memory **vendor-neutrally** (no code path may assume NVIDIA),
with zero new default dependencies, and use it to make model swaps safe — without
changing default behavior (the broker runs on CPU-only and Apple-silicon boxes
with no GPU tool at all).

## Decision

Add an **opt-in, pluggable `ResourceTelemetry` provider interface** selected by
`BROKER_GPU_TELEMETRY` (`off` **default** | `nvidia` | `amd` | `host` | `auto`),
plus a **measured-release** wait on eviction. (`broker/resources.py`,
`broker/resources_probe.py`, `broker/manager.py`, `broker/proc.py`,
`broker/adapters/*`.)

1. **Providers shell out to the stock driver CLI**, argv-only (never a shell
   string), to an **absolute, allow-listed** binary (an explicit absolute
   `BROKER_GPU_TELEMETRY_*_PATH` override, else a fixed candidate list; PATH is
   never consulted), with a **timeout**, parsing **defensively**:
   - `nvidia` — `nvidia-smi --query-gpu=index,name,memory.total,memory.free,memory.used
     --format=csv,noheader,nounits` + `--query-compute-apps=gpu_uuid,pid,used_memory`
     for per-PID holders.
   - `amd` — `amd-smi metric --mem-usage --json` (+ `amd-smi process --json`), with
     a `rocm-smi --showmeminfo vram --json` fallback.
   - `host` — Linux `/proc/meminfo` (`MemAvailable`), macOS `sysctl hw.memsize`/
     `hw.pagesize` + `vm_stat`. One "device" = the host RAM pool (for CPU and
     Apple-silicon unified-memory runtimes).
   - `auto` — probe `nvidia` → `amd` → `host`, first that returns `ok`.
   Each successful sample carries `source: measured`. A missing tool or garbled
   output yields a sample with `status: unavailable` + a human `reason` — **never a
   crash and never an invented number**. Samples are cached for
   `BROKER_GPU_TELEMETRY_POLL_SECONDS`; the blocking probe runs in a thread so the
   event loop is never stalled. Every parser is unit-tested against recorded
   fixtures (multi-GPU, MIG/unsupported `[N/A]` fields, malformed, AMD, rocm-smi,
   Linux `/proc`, macOS `vm_stat`).

2. **Vendor-correct device env var.** `gpu.device_ids` (already in the registry)
   is set into the env var the active provider expects: `amd` →
   `ROCR_VISIBLE_DEVICES` (the ROCm runtime-level isolation var on Linux — research
   source 3), every other provider including the default OFF → `CUDA_VISIBLE_DEVICES`
   (the portable default, unchanged from v0.6.0). `BROKER_GPU_DEVICE_ENV_VAR`
   overrides (e.g. `HIP_VISIBLE_DEVICES`). `BROKER_CUDA_VISIBLE_DEVICES` still works.

3. **Measured release.** After evicting a broker-spawned runtime, the next load on
   the same device(s) waits until (a) the process group is gone **and** (b) — when
   telemetry is on — the device no longer reports that runtime's PID, bounded by
   `BROKER_RELEASE_TIMEOUT_SECONDS`. On timeout the model enters a visible
   **`release_stuck`** lifecycle state (PID, device, MiB held) and the waiting load
   fails with a clear **503** (`ReleaseStuckError`) naming it, rather than starting
   into an OOM. A subsequent load onto a still-stuck device is refused fast by the
   same 503. **With telemetry off, timing equals v0.6.0** — the only change is that
   `ProcessSupervisor.stop()` now **returns a released flag and logs at error level**
   when a process is still alive after SIGKILL (surfaced as `survived_kill`), instead
   of silently returning. The broker **never force-resets a GPU and never kills a
   process it did not start** — it waits and surfaces the stuck holder.

4. **`/status` and `/healthz/backends` gain a `resources` block**: provider +
   status, measured devices (`source: measured`), per-PID holders, declared
   admission budgets + recent decisions (ADR 0022), and any `release_stuck`/
   `survived_kill` holders. Ollama/external-daemon memory is reported **separately**
   under `reported` (`/api/ps` `size_vram`), labeled `reported`, **never mixed** with
   `measured`.

## Alternatives considered

- **NVML / `pynvml`.** Rejected: NVIDIA-only and a build/runtime dependency,
  breaking "no path assumes NVIDIA" and "default OFF, zero new deps".
  `nvidia-smi --query-*` gives total/free/used and per-PID memory with no library.
- **`psutil` for host memory.** Rejected: `/proc/meminfo` and `vm_stat`+`sysctl`
  are stable and dependency-free (and are what psutil reads underneath). Keeps the
  `host` provider zero-dependency.
- **Force a GPU reset / kill the holder on timeout.** Rejected: the holder may be a
  foreign process; resetting a shared GPU is destructive. We surface `release_stuck`
  and fail the load instead.
- **Gate the release wait even with telemetry off.** Rejected: that would change
  v0.6.0 swap timing. With telemetry off we rely on `stop()`'s existing termination
  plus the new survival report.
- **Free-memory recovery as the release gate.** Rejected as the primary signal:
  a zombie keeps the context even after the process is gone, so PID-presence on the
  device is the honest gate (source 8); free memory is reported for visibility.

## Consequences

- Safe model swaps on real GPUs when telemetry is on; identical behavior to v0.6.0
  when off (the full v0.6.0 suite passes on this branch).
- A new vendor-neutral measurement surface other features build on: ADR 0022
  (memory-aware admission) and ADR 0023 (overload-triggered queueing).
- Telemetry depends on external tools whose output can drift; parsing is defensive
  and fixture-tested, and any failure degrades to `unavailable`, never a crash.
- A new `release_stuck` lifecycle state and a `ReleaseStuckError` (503).

## Confirmation

- `tests/test_resources.py` — every parser against recorded fixtures (multi-GPU,
  MIG/`[N/A]`, malformed, AMD + processes, rocm-smi bytes, `/proc/meminfo`
  `MemAvailable`, macOS `vm_stat`); provider degrade-never-crash (`off`, missing
  tool → `unavailable`, real `host` measured); `resolve_tool` rejects a relative
  override; `make_telemetry`; the scriptable `FakeTelemetry`; the `resources_probe`
  CLI exit codes.
- `tests/test_measured_release.py` — eviction waits for a scripted release then
  loads; a never-releasing holder → `release_stuck` + 503 visible in `/status`; a
  second load on a stuck device fails fast; telemetry-off keeps v0.6.0 timing but
  reports `survived_kill`.
- `tests/test_proc_release.py` — `stop()` returns a released flag.
- `tests/test_admission.py` — vendor-correct device env var (`CUDA_VISIBLE_DEVICES`
  default, `ROCR_VISIBLE_DEVICES` for `amd`, explicit override).
- Real `host` smoke recorded in the research note (this Mac, `source: measured`).
