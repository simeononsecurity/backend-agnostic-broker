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

3. **Measured, group- and memory-aware release.** After evicting a broker-spawned
   runtime, the next load on the same device(s) waits until (a) the process group
   is gone **and** (b) — when telemetry is on — **none of that runtime's
   process-group PIDs** still hold device memory **and** the device's **free memory
   has recovered** by ≥ `BROKER_GPU_RELEASE_RECOVERY_FRACTION` (default 0.9) of what
   the runtime held, bounded by `BROKER_RELEASE_TIMEOUT_SECONDS`. Two real cases
   drove this over watching only the parent PID: a runtime whose VRAM is held by
   **child** processes (vLLM's EngineCore / TP workers), and a **container**
   deployment where `nvidia-smi` lists host-namespace or no PIDs — in both the
   parent PID is absent yet the memory is still out. The group is the pgid recorded
   at spawn (`/proc/*/stat` scan on Linux; best effort elsewhere); where no PID can
   be attributed at all, memory recovery alone decides. On timeout the model enters
   a visible **`release_stuck`** state (PID/group, device, MiB held) and the waiting
   load fails with a clear **503** (`ReleaseStuckError`), rather than starting into
   an OOM. **`release_stuck` auto-clears**: each later load attempt on that device,
   and the periodic idle/telemetry tick, re-checks the holder and transitions it
   `release_stuck → unloaded` once its group PIDs are gone and memory has returned
   (no restart needed); a load for the stuck model itself is refused until then. A
   subsequent load onto a still-stuck device is refused fast by the same 503.
   **With telemetry off, timing equals v0.6.0** — the only change is that
   `ProcessSupervisor.stop()` now **returns a released flag and logs at error level**
   when a process is still alive after SIGKILL (surfaced as `survived_kill`), instead
   of silently returning. The broker **never force-resets a GPU and never kills a
   process it did not start** — it waits and surfaces the stuck holder.

   > **Container caveat.** Inside a container whose PID namespace hides the GPU
   > holders, release is judged by free-memory recovery alone. If another tenant
   > immediately claims the freed memory, recovery can read as incomplete and the
   > model is (conservatively) flagged `release_stuck` with a clear 503 rather than
   > risking an OOM; it auto-clears once free memory returns.

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
- **Free-memory recovery as the *sole* release gate.** Rejected as the *only*
  signal: a zombie can keep the context after the process is gone, so group-PID
  presence is checked too. But watching PIDs *alone* was also wrong (children and
  container PID namespaces hide the holder), so release now requires **both** —
  group PIDs absent **and** free memory recovered — with memory recovery as the
  fallback when no PID can be attributed (source 8).

## Consequences

- Safe model swaps on real GPUs when telemetry is on; identical behavior to v0.6.0
  when off (the full v0.6.0 suite passes on this branch).
- A new vendor-neutral measurement surface other features build on: ADR 0022
  (memory-aware admission) and ADR 0023 (overload-triggered queueing).
- Telemetry depends on external tools whose output can drift; parsing is defensive
  and fixture-tested, and any failure degrades to `unavailable`, never a crash.
- A new `release_stuck` lifecycle state and a `ReleaseStuckError` (503), which
  **auto-clears** back to `unloaded` once the device memory returns (on the next
  load attempt or the periodic tick) — never a restart-to-recover.

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
- `tests/test_resources_followup.py` — group/memory-aware release: a child PID
  (vLLM worker) and a no-PID container view both keep the device held → 503 (not a
  false release); `release_stuck` auto-recovers to `unloaded` once memory returns;
  a normal release still proceeds promptly; plus the atomic-admission and
  no-needless-eviction cases (see ADR 0022).
- `tests/test_proc_release.py` — `stop()` returns a released flag.
- `tests/test_admission.py` — vendor-correct device env var (`CUDA_VISIBLE_DEVICES`
  default, `ROCR_VISIBLE_DEVICES` for `amd`, explicit override).
- Real `host` smoke recorded in the research note (this Mac, `source: measured`).
