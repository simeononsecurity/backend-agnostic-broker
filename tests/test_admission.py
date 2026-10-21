"""ADR 0022 memory-aware admission + the vendor-correct device env var (ADR 0020).

Declared accounting (telemetry off, a BROKER_GPU_MEMORY_BUDGET_MIB budget) and
measured accounting (fake telemetry free memory) both fit / evict / block a sized
model; unsized models keep v0.6.0 behavior.
"""

from __future__ import annotations

import pytest

from broker.config import Settings
from broker.errors import CapacityUnavailableError
from broker.lifecycle import State
from broker.manager import ModelManager
from broker.registry import BackendType, GPURequirements, ModelEntry, load_registry
from broker.resources import DeviceMemory, FakeTelemetry


def _manager(settings: Settings) -> ModelManager:
    reg = load_registry(settings.registry_path, set(settings.enabled_backends))
    m = ModelManager(reg, settings)
    m.validate_all()
    # Give both fake models a declared footprint for admission tests.
    for mm in m.models.values():
        mm.entry.memory_mib = 600
    return m


# --- declared accounting (telemetry off) -----------------------------------
async def test_declared_admission_evicts_to_fit(settings: Settings):
    settings.max_loaded_models = 5  # count limit out of the way
    settings.gpu_memory_budget_mib = "1000"  # per-device budget; two 600s don't fit
    mgr = _manager(settings)
    await mgr.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    await mgr.chat("echo2", {"messages": [{"role": "user", "content": "y"}]}, "r2")
    # echo (600) was evicted so echo2 (600) fits the 1000 MiB budget.
    assert mgr.models["echo"].machine.state == State.UNLOADED
    assert mgr.models["echo2"].machine.state in (State.READY, State.BUSY)
    decisions = [a["decision"] for a in mgr._admissions]
    assert any(d.startswith("evicted") for d in decisions)
    await mgr.shutdown()


async def test_declared_admission_blocks_when_holder_busy(settings: Settings):
    settings.max_loaded_models = 5
    settings.gpu_memory_budget_mib = "1000"
    mgr = _manager(settings)
    # Pin echo as busy (in-flight) so it cannot be evicted.
    await mgr.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    mgr.models["echo"].machine.inflight = 1
    with pytest.raises(CapacityUnavailableError) as ei:
        await mgr.chat("echo2", {"messages": [{"role": "user", "content": "y"}]}, "r2")
    assert ei.value.http_status == 429
    assert ei.value.reason == "declared_insufficient"
    assert "echo" in (ei.value.holder or "")
    mgr.models["echo"].machine.inflight = 0
    await mgr.shutdown()


async def test_sized_model_without_budget_or_telemetry_keeps_v060(settings: Settings):
    # memory_mib set but neither telemetry nor a declared budget -> count-only.
    settings.max_loaded_models = 5
    mgr = _manager(settings)
    await mgr.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    await mgr.chat("echo2", {"messages": [{"role": "user", "content": "y"}]}, "r2")
    assert mgr.models["echo"].machine.state in (State.READY, State.BUSY)
    assert mgr.models["echo2"].machine.state in (State.READY, State.BUSY)
    assert len(mgr._admissions) == 0  # admission never engaged
    await mgr.shutdown()


# --- measured accounting (fake telemetry) ----------------------------------
async def test_measured_admission_fits(settings: Settings):
    settings.max_loaded_models = 5
    settings.gpu_memory_headroom_mib = 1024
    mgr = _manager(settings)
    mgr.telemetry = FakeTelemetry(
        [DeviceMemory(index="default", name="GPU 0", total_mib=8000, free_mib=5000)]
    )
    await mgr.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    assert mgr.models["echo"].machine.state in (State.READY, State.BUSY)
    assert mgr._admissions[-1]["detail"]["reason"] == "measured_fits"
    await mgr.shutdown()


async def test_measured_admission_blocks_when_device_full(settings: Settings):
    settings.max_loaded_models = 5
    settings.gpu_memory_headroom_mib = 1024
    mgr = _manager(settings)
    # 1000 free - 1024 headroom < 600 needed, and no idle victim exists.
    mgr.telemetry = FakeTelemetry(
        [DeviceMemory(index="default", name="GPU 0", total_mib=8000, free_mib=1000)]
    )
    with pytest.raises(CapacityUnavailableError) as ei:
        await mgr.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    assert ei.value.reason == "measured_insufficient"
    await mgr.shutdown()


async def test_status_resources_block_shape(settings: Settings):
    settings.gpu_memory_budget_mib = "40000"
    mgr = _manager(settings)
    block = (await mgr.status())["resources"]
    assert block["telemetry"]["provider"] == "off"
    assert block["telemetry"]["enabled"] is False
    assert block["admission"]["budget_mib"] == {"*": 40000}
    assert block["source"] is None
    await mgr.shutdown()


# --- vendor-correct device env var (ADR 0020 item 2) -----------------------
def _vllm_entry() -> ModelEntry:
    return ModelEntry(
        alias="m",
        backend=BackendType.VLLM,
        runtime_model_id="x",
        base_url="http://127.0.0.1:8001",
        gpu=GPURequirements(device_ids=[0, 1]),
    )


def test_default_provider_uses_cuda_visible_devices(settings: Settings):
    from broker.adapters.vllm import VLLMAdapter

    ad = VLLMAdapter(_vllm_entry(), settings)
    assert ad._process_env() == {"CUDA_VISIBLE_DEVICES": "0,1"}


def test_amd_provider_uses_rocr_visible_devices(settings: Settings):
    from broker.adapters.vllm import VLLMAdapter

    settings.gpu_telemetry = "amd"
    ad = VLLMAdapter(_vllm_entry(), settings)
    assert ad._process_env() == {"ROCR_VISIBLE_DEVICES": "0,1"}


def test_explicit_env_var_override(settings: Settings):
    from broker.adapters.vllm import VLLMAdapter

    settings.gpu_device_env_var = "HIP_VISIBLE_DEVICES"
    ad = VLLMAdapter(_vllm_entry(), settings)
    assert ad._process_env() == {"HIP_VISIBLE_DEVICES": "0,1"}


# --- the examples/gpu-memory/ example actually works -----------------------
async def test_example_gpu_memory_registry_admits_and_evicts(tmp_models_root):
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    example = repo / "examples" / "gpu-memory"
    s = Settings(
        auth_enabled=False,
        registry_path=str(example / "registry.yaml"),
        enabled_backends=["fake"],
        allowed_model_roots=[str(tmp_models_root)],
        max_loaded_models=5,
        gpu_memory_budget_mib="1000",  # mirrors the example's .env
    )
    reg = load_registry(s.registry_path, {"fake"})
    mgr = ModelManager(reg, s)
    mgr.validate_all()
    await mgr.chat("small-a", {"messages": [{"role": "user", "content": "hi"}]}, "r1")
    await mgr.chat("small-b", {"messages": [{"role": "user", "content": "hi"}]}, "r2")
    # Two 600 MiB models can't coexist in a 1000 MiB budget -> small-a evicted.
    assert mgr.models["small-a"].machine.state == State.UNLOADED
    assert mgr.models["small-b"].machine.state in (State.READY, State.BUSY)
    await mgr.shutdown()
