# ADR 0022: Memory-aware admission (sized models)

Status: Accepted
Date: 2026-10-20
Supersedes: —
Related: ADR 0020 (resource telemetry + measured release), ADR 0023 (request
queueing), ADR 0003 (pools/sticky/health), `docs/research/adr-0020-resource-telemetry-and-admission.md`

## Context

The broker caps residency by **count** (`max_loaded_models`): on load it evicts the
LRU idle model and proceeds; if none is evictable it returns a 429
(`ConcurrencyLimitError`). Count is a poor proxy for GPU memory — three 6 GiB models
and three 70 GiB models both "count" as three. On real hardware a count-legal load
can still OOM, and there is no typed signal a future queue (ADR 0023) can wait on to
tell "capacity unavailable" apart from a generic concurrency 429.

ADR 0020 adds opt-in, vendor-neutral measured memory and a measured-release wait.
This ADR uses it (and a declared fallback) to make admission **memory-aware** for
models that opt in, while leaving unsized models exactly as in v0.6.0.

## Decision

Add an **opt-in, per-model `memory_mib`** (registry `ModelEntry`) and a
memory-aware admission step in front of loading (`broker/manager.py`,
`broker/registry.py`, `broker/config.py`, `broker/errors.py`).

1. **Budget per device.**
   - **Telemetry on** (ADR 0020): available = measured free memory −
     `BROKER_GPU_MEMORY_HEADROOM_MIB` on the model's device(s).
   - **Telemetry off**: available = declared `BROKER_GPU_MEMORY_BUDGET_MIB` −
     Σ `memory_mib` of resident models on the same device. The budget is a single
     integer (every device) or a per-device map (`0=24000,1=48000`; key `default`
     covers unpinned models). Measured and declared accounting are **never
     conflated** and are labeled distinctly in `/status`.
   A model's device keys are `gpu.device_ids` (as strings) or a shared `default`
   bucket when unpinned. For measured unpinned models, best-fit picks the device
   with the most free memory; pinned models must fit each pinned device.

2. **Admission (atomic, with reservations).** The whole admit → evict → reserve
   decision runs under one global admission lock, so two concurrent loads cannot
   both measure the same free memory and double-book a device. In **measured** mode
   the memory of models still in `loading` (and, for a grace window, just-`ready`
   ones the driver's telemetry has not yet reflected) is subtracted from free as an
   **outstanding reservation**. Before loading a model with `memory_mib`:
   - if the need **exceeds the device's total capacity** (declared budget, or
     measured total − headroom) it can never fit → fail fast with
     `CapacityUnavailableError`, `reason=exceeds_device_capacity`, evicting nothing;
   - otherwise **evict LRU idle models on the same device(s)** — each eviction
     followed by the ADR 0020 measured-release wait — but **only while evicting the
     evictable candidates could actually make it fit** (no needless eviction); if it
     still won't fit, raise `CapacityUnavailableError` with `reason`
     (`measured_insufficient` / `declared_insufficient`) and `holder`.

   Count-based `max_loaded_models` **stays** as an additional limit, applied after
   memory admission. Models **without** `memory_mib`, or with `memory_mib` but
   neither telemetry nor a declared budget configured, keep exactly v0.6.0
   count-only behavior.

3. **Typed capacity signal for ADR 0023.** `CapacityUnavailableError` subclasses
   `ConcurrencyLimitError` (so existing 429 handling is unchanged) but is a distinct
   type with `reason` + `holder`, so the request-queue card can wait on a clean
   "capacity unavailable" predicate instead of a bare 429 string. Its type, HTTP
   status (429), `reason`, `holder` and envelope are unchanged by this follow-up —
   only a new `reason` **value** (`exceeds_device_capacity`) is added, which a
   waiting queue must treat as non-waitable (the model can never fit). Admission
   decisions (`fits` / `evicted X` / `blocked by Y`) are recorded and exposed in the
   `/status` `resources.admission` block and logs.

## Alternatives considered

- **Infer memory from the model file size / a heuristic.** Rejected: dishonest
  (KV cache, CUDA graphs, fragmentation make it wrong) and it would present an
  estimate as if it were a measurement. `memory_mib` is an explicit operator
  declaration; measured free memory is used whenever telemetry is on.
- **Always require telemetry for sizing.** Rejected: operators on heterogeneous or
  no-telemetry hosts still benefit from declared bookkeeping; telemetry refines it
  to measured when available.
- **Reuse the generic 429 for capacity.** Rejected: ADR 0023 needs to *wait* on
  capacity (not fail), which requires a distinct, typed predicate.
- **A full bin-packing scheduler across devices.** Rejected as over-scoped: LRU-idle
  eviction on the model's own device(s) plus best-fit for unpinned models is simple,
  provable, and composes with the existing count limit; richer placement is left to
  the registry's existing `device_ids` pinning.

## Consequences

- Sized models load only when they actually fit (measured) or fit the declared
  budget — fewer OOM load failures on shared GPUs.
- A clean typed capacity signal (`CapacityUnavailableError` + `reason`/`holder`)
  that ADR 0023's queue builds on.
- Unsized models and telemetry-off/no-budget deployments are byte-for-byte v0.6.0.
- Operators must set honest `memory_mib` and a budget (or telemetry) for the
  feature to engage; mis-sized declarations can still over/under-admit — documented
  in the guide and surfaced via `/status` admission decisions.

## Confirmation

- `tests/test_admission.py` — declared accounting evicts an idle holder to fit;
  blocks (429, `reason=declared_insufficient`, `holder` names the busy model) when
  the holder can't be evicted; a sized model with no budget/telemetry keeps v0.6.0
  count-only; measured accounting fits and blocks (`reason=measured_insufficient`);
  the `/status` `resources` block shape (provider, enabled, budget map, source).
- `tests/test_measured_release.py` — admission eviction uses the ADR 0020 measured
  release wait.
- `tests/test_resources_followup.py` — concurrent admission does not overcommit,
  declared (P1) and measured (P2); an impossible request (need > device capacity)
  fails fast with `reason=exceeds_device_capacity` and evicts nothing (P5).
- `tests/test_registry.py` / `tests/test_config.py` — `memory_mib` validation and
  `BROKER_GPU_MEMORY_*` parsing/validation.
