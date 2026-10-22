"""Required-GPU admission: device inventory + the pure pre-load check (ADR 0025).

This module holds the *vendor-neutral, side-effect-free* pieces of enforcing
``gpu.required`` / ``gpu.device_ids``: resolving the device inventory the broker
may place on, and the deterministic pre-load decision. The manager
(:mod:`broker.manager`) wires these into the shared admission path — it samples
telemetry, raises the typed errors, runs post-load verification, and unloads on
failure. Keeping the decision logic here makes it unit-testable without a manager
and gives ADR 0026 (device leases) a clean seam to extend.

Inventory sources, in order (labelled so ``/status`` can show how placement was
established, never a mixed/invented figure):

* ``measured``  — the telemetry provider's devices, when telemetry is ON and the
  sample is ``ok``.
* ``declared``  — ``BROKER_GPU_DEVICES`` (ordinals), falling back to
  ``BROKER_CUDA_VISIBLE_DEVICES``.
* ``none``      — telemetry OFF/unavailable and nothing declared.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .registry import GPURequirements
from .resources import TelemetrySample


@dataclass(frozen=True)
class GpuInventory:
    """The devices the broker may place a required model on, plus their source."""

    devices: tuple[str, ...] = ()
    source: str = "none"  # "measured" | "declared" | "none"

    def has(self, device_key: str) -> bool:
        return str(device_key) in self.devices

    @property
    def empty(self) -> bool:
        return not self.devices


def resolve_inventory(
    *,
    telemetry_on: bool,
    sample: TelemetrySample | None,
    declared: list[str] | None,
) -> GpuInventory:
    """Resolve the device inventory from a (already-taken) telemetry sample + config.

    Pure: the manager owns the async sampling and passes the result in. When
    telemetry is on but the sample is unavailable we fall through to the declared
    list (an honest "no measurement", never an invented device set).
    """
    if telemetry_on and sample is not None and sample.ok:
        return GpuInventory(
            devices=tuple(str(d.index) for d in sample.devices),
            source="measured",
        )
    decl = [str(d).strip() for d in (declared or []) if str(d).strip()]
    if decl:
        return GpuInventory(devices=tuple(decl), source="declared")
    return GpuInventory(devices=(), source="none")


@dataclass(frozen=True)
class PreloadProblem:
    """Why a required model cannot be admitted to a GPU (pre-load, deterministic)."""

    reason: str  # "empty_inventory" | "unknown_device_id"
    summary: str
    missing: tuple[str, ...] = field(default_factory=tuple)


def preload_problem(gpu: GPURequirements, inventory: GpuInventory) -> PreloadProblem | None:
    """Return a :class:`PreloadProblem` if ``gpu`` cannot be placed, else ``None``.

    Enforcement applies only to entries that set ``gpu.required`` (a model without
    it keeps exact pre-ADR-0025 behaviour). A required model fails when the
    inventory is empty, or when any pinned ``device_ids`` entry is absent from it.
    """
    if not gpu.required:
        return None
    if inventory.empty:
        return PreloadProblem(
            reason="empty_inventory",
            summary=(
                "gpu.required is set but no GPU is available: telemetry is off or "
                "unavailable and no device inventory is declared "
                "(BROKER_GPU_DEVICES / BROKER_CUDA_VISIBLE_DEVICES)"
            ),
        )
    wanted = [str(d) for d in gpu.device_ids]
    missing = tuple(d for d in wanted if not inventory.has(d))
    if missing:
        return PreloadProblem(
            reason="unknown_device_id",
            summary=(
                f"gpu.device_ids {list(missing)} not in the {inventory.source} "
                f"inventory {list(inventory.devices)}"
            ),
            missing=missing,
        )
    return None


def required_device_keys(gpu: GPURequirements, inventory: GpuInventory) -> list[str]:
    """Devices a required model must demonstrably hold memory on, post-load.

    The pinned ``device_ids`` when set, else every device in the inventory (the
    model may land on any of them).
    """
    if gpu.device_ids:
        return [str(d) for d in gpu.device_ids]
    return list(inventory.devices)
