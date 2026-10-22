# ADR 0025: Enforce `gpu.required` at load time in every production adapter

Status: Proposed
Date: 2026-10-21
Supersedes: —
Related: ADR 0020 (vendor-neutral telemetry), ADR 0022 (memory-aware admission),
ADR 0026 (device leases), `docs/gpu-placement.md`

## Context

`docs/gpu-placement.md` documents the registry field `gpu.required` as "Require a GPU" and
"fail if unavailable", and it documents `gpu.device_ids` as device selection. A production
review found that only the fake test adapter reads `gpu.required`. The Ollama, vLLM,
llama.cpp and Strata adapters ignore it, and nothing checks that `gpu.device_ids` exist.

A model marked required can therefore:

- start on CPU, silently and slowly;
- start on a device the operator did not intend;
- fail late inside the runtime with an unhelpful error.

The gap between the docs and the behaviour is itself a defect: operators planned GPU use
around a guarantee the broker did not give.

The difficulty is that the broker can establish GPU presence in different ways per backend:

- measured telemetry (ADR 0020), when enabled;
- a declared device inventory (config);
- the backend's own report (Ollama `/api/ps` `size_vram`);
- for an external OpenAI-compatible endpoint, nothing at all.

The check must stay vendor-neutral and must never invent a measurement.

## Decision

We will enforce `gpu.required` and `gpu.device_ids` in the shared admission path, before any
adapter spawns or loads, and verify placement after the load where the backend allows it.

1. **Device inventory.** The broker resolves the devices it may use, in this order:
   - the telemetry provider's measured devices (when on);
   - otherwise a declared list: a new setting `BROKER_GPU_DEVICES` (ordinals), falling back
     to `BROKER_CUDA_VISIBLE_DEVICES`.

   Each source is labelled `measured` or `declared`.
2. **Pre-load check (all adapters).** A load fails fast, with nothing spawned or evicted, and
   raises the typed error `gpu_unavailable` (HTTP 503, not retried on the same member, while a
   pool may fail over to a member that qualifies) when either holds:
   - `gpu.required` is true and the inventory is empty;
   - a `gpu.device_ids` entry is not in the inventory.

   No CPU fallback happens.
3. **Post-load verification where possible:**
   - **Broker-managed runtimes with measured telemetry:** the runtime's process group must
     hold memory on a required device within `BROKER_GPU_VERIFY_SECONDS` (default 30) of
     becoming ready. Otherwise it is unloaded, and the request fails with
     `gpu_placement_failed`.
   - **Ollama:** after load, `/api/ps` must report `size_vram > 0` for the model. Otherwise
     the model is unloaded and the request fails with `gpu_placement_failed`. The status
     shows the evidence as `reported`.
   - **External OpenAI-compatible endpoints (no lifecycle):** the broker cannot verify
     placement. `gpu.required: true` there is a **startup configuration error**, unless the
     entry also sets `gpu.verify: declared`, an explicit operator statement that is shown as
     `declared` in `/status`.
4. **Visibility.** `/status` shows, per model, `gpu.required`, the inventory source and the
   last verification result. The docs and `registry.example.yaml` describe exactly what is
   enforced per backend.

Models without `gpu.required` behave as before. Enforcement applies only to entries that
already claim the requirement, so it closes a documented promise and is not opt-in.

## Alternatives considered

- **Change the docs to say the field is advisory.** Rejected: operators rely on the
  guarantee, and an advisory field that quietly allows CPU fallback is the failure mode we
  saw.
- **Trust runtime flags (e.g. `--n-gpu-layers`) as proof.** Rejected: flags state intent;
  a runtime can still fall back to CPU or land on the wrong device.
- **Require telemetry for every required model.** Rejected: telemetry is opt-in and
  vendor-specific underneath. A declared inventory keeps the pre-load check useful
  everywhere, and verification is used where evidence exists.
- **Silently ignore `gpu.required` on external endpoints.** Rejected: that repeats the
  original gap. A startup error with an explicit `declared` escape hatch is honest.

## Consequences

- **Positive:**
  - A required-GPU model never runs on CPU unnoticed.
  - Bad device ids fail at the first load with a clear error.
  - `/status` shows how placement was established.
- **Negative:**
  - Existing registries that set `gpu.required: true` on external endpoints, or reference
    missing devices, now fail at startup or first load. This is a deliberate behaviour change
    for misconfigured entries; the CHANGELOG and upgrading guide call it out.
  - Post-load verification adds up to `BROKER_GPU_VERIFY_SECONDS` to a cold load in the
    failure case.
- **Neutral:**
  - New settings `BROKER_GPU_DEVICES` and `BROKER_GPU_VERIFY_SECONDS`, the registry value
    `gpu.verify: declared`, and the error types `gpu_unavailable` and `gpu_placement_failed`.
  - Exclusive use of a device is a separate decision (ADR 0026).

## Confirmation

ADR 0027 drills D7–D8, plus unit tests per adapter:

- an empty inventory or an unknown device id fails before spawn with nothing evicted;
- the Ollama `size_vram == 0` case unloads and fails;
- an external entry with `required` and no `verify: declared` refuses to start;
- a measured provider fixture with no device memory after ready unloads.
