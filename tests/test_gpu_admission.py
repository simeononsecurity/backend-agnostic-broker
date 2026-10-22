"""ADR 0025: enforce gpu.required / gpu.device_ids at load in every adapter.

Fake backend + scriptable FakeTelemetry (no GPU): the pre-load inventory gate
runs before any spawn/eviction, post-load verification proves placement where the
backend allows it (measured telemetry / Ollama size_vram), external required
entries are a startup config error unless gpu.verify: declared, and a required
member fails over to a qualifying one.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from broker.config import Settings
from broker.errors import ConfigError, GpuPlacementFailedError, GpuUnavailableError
from broker.lifecycle import State
from broker.manager import ModelManager
from broker.registry import load_registry
from broker.resources import DeviceMemory, FakeTelemetry


def _settings(tmp_path: Path, registry_yaml: str, **over) -> Settings:
    reg = tmp_path / "registry.yaml"
    reg.write_text(textwrap.dedent(registry_yaml).strip())
    kw = dict(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(reg),
        enabled_backends=["fake", "ollama", "vllm", "llamacpp", "strata"],
        allowed_model_roots=[str(tmp_path)],
        allowed_exec_dirs=["/usr/bin", "/usr/local/bin", "/bin"],
        model_idle_timeout_seconds=60,
        idle_sweep_interval_seconds=5,
        max_loaded_models=2,
    )
    kw.update(over)
    return Settings(**kw)


def _manager(settings: Settings) -> ModelManager:
    reg = load_registry(settings.registry_path, set(settings.enabled_backends))
    mgr = ModelManager(reg, settings)
    mgr.validate_all()
    return mgr


class _MeasuredTelemetry(FakeTelemetry):
    """Reports device 0; attributes ``held_mib`` to the live runtime's PID.

    ``held_mib == 0`` → the device holds no memory for the runtime (placement
    failed). Reads the pid live each sample so it works regardless of the
    fake adapter's synthetic PID.
    """

    def __init__(self, mgr: ModelManager, alias: str, held_mib: int) -> None:
        super().__init__()
        self._mgr = mgr
        self._alias = alias
        self._held = held_mib

    def sample(self):  # type: ignore[override]
        self.samples_taken += 1
        pid = self._mgr.models[self._alias].adapter.process_pid()
        procs = {pid: self._held} if (pid and self._held > 0) else {}
        self.devices = [
            DeviceMemory(index="0", name="GPU 0", total_mib=8000, free_mib=4000, processes=procs)
        ]
        return FakeTelemetry.sample(self)


# --------------------------------------------------------------------------- #
# Pre-load gate: empty inventory / unknown device id fail before any spawn.
# --------------------------------------------------------------------------- #
async def test_required_empty_inventory_fails_before_spawn(tmp_path):
    settings = _settings(
        tmp_path,
        """
        models:
          - alias: req
            backend: fake
            runtime_model_id: fake-req
            gpu:
              required: true
        """,
    )
    mgr = _manager(settings)
    with pytest.raises(GpuUnavailableError) as ei:
        await mgr.chat("req", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    assert ei.value.http_status == 503
    assert ei.value.error_type == "gpu_unavailable"
    m = mgr.models["req"]
    # Nothing spawned: the fake adapter's start() was never called.
    assert m.adapter._load_count == 0  # type: ignore[attr-defined]
    assert m.machine.state == State.UNLOADED
    await mgr.shutdown()


async def test_required_unknown_device_id_fails_before_spawn_and_no_eviction(tmp_path):
    settings = _settings(
        tmp_path,
        """
        models:
          - alias: resident
            backend: fake
            runtime_model_id: fake-res
          - alias: req
            backend: fake
            runtime_model_id: fake-req
            gpu:
              required: true
              device_ids: [7]
        """,
        gpu_devices="0,1",  # declared inventory has 0 and 1, not 7
        max_loaded_models=1,  # loading req would otherwise need to evict resident
    )
    mgr = _manager(settings)
    # Warm an idle model occupying the single load slot.
    await mgr.chat("resident", {"messages": [{"role": "user", "content": "x"}]}, "r0")
    assert mgr.models["resident"].machine.state in (State.READY, State.BUSY)

    with pytest.raises(GpuUnavailableError) as ei:
        await mgr.chat("req", {"messages": [{"role": "user", "content": "y"}]}, "r1")
    assert "7" in str(ei.value)
    # The resident model was NOT evicted and req never spawned.
    assert mgr.models["resident"].machine.state in (State.READY, State.BUSY)
    assert mgr.models["req"].adapter._load_count == 0  # type: ignore[attr-defined]
    await mgr.shutdown()


async def test_required_device_id_present_in_declared_inventory_loads(tmp_path):
    settings = _settings(
        tmp_path,
        """
        models:
          - alias: req
            backend: fake
            runtime_model_id: fake-req
            gpu:
              required: true
              device_ids: [0]
        """,
        gpu_devices="0,1",
    )
    mgr = _manager(settings)
    # Telemetry off → declared inventory contains 0 → preload passes; measured
    # verification is skipped (no measurement), the model serves.
    out = await mgr.chat("req", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    assert out["choices"][0]["message"]["content"].startswith("echo:")
    v = mgr.models["req"].gpu_verification
    assert v["inventory_source"] == "declared"
    assert v["result"] == "unverified"  # telemetry off: honest no-measurement
    await mgr.shutdown()


# --------------------------------------------------------------------------- #
# Post-load measured verification (telemetry on).
# --------------------------------------------------------------------------- #
async def test_measured_no_device_memory_after_ready_unloads(tmp_path):
    settings = _settings(
        tmp_path,
        """
        models:
          - alias: req
            backend: fake
            runtime_model_id: fake-req
            gpu:
              required: true
              device_ids: [0]
        """,
        gpu_verify_seconds=0.2,
        gpu_telemetry_poll_seconds=0.02,
    )
    mgr = _manager(settings)
    mgr.telemetry = _MeasuredTelemetry(mgr, "req", held_mib=0)  # never holds memory
    assert mgr.telemetry_enabled()
    with pytest.raises(GpuPlacementFailedError) as ei:
        await mgr.chat("req", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    assert ei.value.http_status == 503
    assert ei.value.error_type == "gpu_placement_failed"
    # The model was unloaded, not left serving on CPU.
    assert mgr.models["req"].machine.state == State.UNLOADED
    assert mgr.models["req"].gpu_verification["result"] == "gpu_placement_failed"
    await mgr.shutdown()


async def test_measured_device_memory_present_passes(tmp_path):
    settings = _settings(
        tmp_path,
        """
        models:
          - alias: req
            backend: fake
            runtime_model_id: fake-req
            gpu:
              required: true
              device_ids: [0]
        """,
        gpu_verify_seconds=1.0,
        gpu_telemetry_poll_seconds=0.02,
    )
    mgr = _manager(settings)
    mgr.telemetry = _MeasuredTelemetry(mgr, "req", held_mib=512)
    out = await mgr.chat("req", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    assert out["choices"][0]["message"]["content"].startswith("echo:")
    v = mgr.models["req"].gpu_verification
    assert v["result"] == "ok"
    assert "512" in v["detail"]
    await mgr.shutdown()


# --------------------------------------------------------------------------- #
# Ollama reported verification via /api/ps size_vram.
# --------------------------------------------------------------------------- #
def _ollama_settings(tmp_path: Path, **over) -> Settings:
    return _settings(
        tmp_path,
        """
        models:
          - alias: oll
            backend: ollama
            runtime_model_id: llama3
            base_url: http://127.0.0.1:11434
            gpu:
              required: true
        """,
        gpu_devices="0",  # declared inventory so the pre-load gate passes
        **over,
    )


async def _patch_ollama_loadable(mgr: ModelManager, size_vram: int):
    """Make the ollama adapter loadable offline and script its /api/ps size_vram."""
    ad = mgr.models["oll"].adapter

    async def _start():
        return None

    async def _ready(timeout):
        return None

    async def _ps():
        return {"models": [{"model": "llama3", "size_vram": size_vram}]}

    async def _stop():
        return None

    async def _chat(payload, request_id):
        return {
            "id": "chatcmpl-x",
            "object": "chat.completion",
            "model": "oll",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hi"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    ad.start = _start  # type: ignore[assignment]
    ad.wait_until_ready = _ready  # type: ignore[assignment]
    ad.ps = _ps  # type: ignore[assignment]
    ad.stop = _stop  # type: ignore[assignment]
    ad.unload = _stop  # type: ignore[assignment]
    ad.chat = _chat  # type: ignore[assignment]


async def test_ollama_size_vram_zero_unloads_and_fails(tmp_path):
    mgr = _manager(_ollama_settings(tmp_path))
    await _patch_ollama_loadable(mgr, size_vram=0)
    with pytest.raises(GpuPlacementFailedError) as ei:
        await mgr.chat("oll", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    assert ei.value.http_status == 503
    assert mgr.models["oll"].machine.state == State.UNLOADED
    v = mgr.models["oll"].gpu_verification
    assert v["result"] == "gpu_placement_failed"
    assert v["evidence"] == "reported"
    await mgr.shutdown()


async def test_ollama_size_vram_positive_passes(tmp_path):
    mgr = _manager(_ollama_settings(tmp_path))
    await _patch_ollama_loadable(mgr, size_vram=4096)
    out = await mgr.chat("oll", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    assert out["model"] == "oll"
    v = mgr.models["oll"].gpu_verification
    assert v["result"] == "ok"
    assert v["evidence"] == "reported"
    await mgr.shutdown()


# --------------------------------------------------------------------------- #
# External OpenAI-compatible endpoint: required is a startup config error
# unless gpu.verify: declared.
# --------------------------------------------------------------------------- #
def _external_yaml(verify_line: str) -> str:
    return f"""
        models:
          - alias: ext
            backend: vllm
            runtime_model_id: ext-model
            base_url: http://127.0.0.1:8000
            gpu:
              required: true
              {verify_line}
        """


async def test_external_required_without_declared_refuses_to_start(tmp_path):
    settings = _settings(tmp_path, _external_yaml(verify_line="# no verify"))
    with pytest.raises(ConfigError) as ei:
        _manager(settings)
    assert "verify" in str(ei.value).lower() or "declared" in ei.value.remediation


async def test_external_required_with_declared_starts_and_shows_declared(tmp_path):
    settings = _settings(tmp_path, _external_yaml(verify_line="verify: declared"))
    mgr = _manager(settings)  # validate_all passes
    m = mgr.models["ext"]
    assert m.adapter.gpu_placement_evidence() == "external"
    # Pre-load: declared short-circuits the inventory gate (no gpu_unavailable).
    await mgr._gpu_preload_check(m)
    assert m.gpu_verification["result"] == "declared"
    # Post-load verification also reports declared without probing anything.
    await mgr._gpu_postload_verify(m)
    assert m.gpu_verification["result"] == "declared"
    await mgr.shutdown()


# --------------------------------------------------------------------------- #
# gpu.verify: declared must NOT be an off-switch on a verifiable backend.
# A broker-managed (measured) or Ollama (reported) entry that sets it is a
# startup ConfigError — otherwise required-GPU enforcement is silently disabled
# and the model loads on CPU with an empty inventory (the reopened defect).
# --------------------------------------------------------------------------- #
async def test_verify_declared_on_managed_backend_is_config_error(tmp_path):
    settings = _settings(
        tmp_path,
        """
        models:
          - alias: managed
            backend: fake
            runtime_model_id: fake-managed
            gpu:
              required: true
              verify: declared
        """,
        # Telemetry off + no declared inventory: without the guard this would
        # load on CPU despite gpu.required.
    )
    with pytest.raises(ConfigError) as ei:
        _manager(settings)
    assert "verify" in str(ei.value).lower()
    # The fake backend stands in for a broker-managed runtime (evidence measured).
    assert "declared" in (ei.value.remediation or "")


async def test_verify_declared_without_required_on_managed_is_config_error(tmp_path):
    # Even without gpu.required, verify: declared on a verifiable backend is an
    # error: it has no legitimate use there and must not be silently accepted.
    settings = _settings(
        tmp_path,
        """
        models:
          - alias: managed
            backend: fake
            runtime_model_id: fake-managed
            gpu:
              verify: declared
        """,
    )
    with pytest.raises(ConfigError):
        _manager(settings)


# --------------------------------------------------------------------------- #
# Pool failover past a gpu_unavailable member to a qualifying one.
# --------------------------------------------------------------------------- #
async def test_pool_failover_past_gpu_unavailable_member(tmp_path):
    settings = _settings(
        tmp_path,
        """
        models:
          - alias: bad
            backend: fake
            runtime_model_id: fake-bad
            pool: p
            gpu:
              required: true
              device_ids: [9]
          - alias: good
            backend: fake
            runtime_model_id: fake-good
            pool: p
            gpu:
              required: true
              device_ids: [0]
        """,
        gpu_devices="0,1",
        route_strategy="least_busy",
    )
    mgr = _manager(settings)
    out = await mgr.chat("p", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    # Served by the qualifying member; the unqualified one never spawned.
    assert out["choices"][0]["message"]["content"] == "echo: good"
    assert mgr.models["bad"].adapter._load_count == 0  # type: ignore[attr-defined]
    assert mgr.models["good"].machine.state in (State.READY, State.BUSY)
    await mgr.shutdown()


async def test_pool_all_members_gpu_unavailable_propagates(tmp_path):
    settings = _settings(
        tmp_path,
        """
        models:
          - alias: a
            backend: fake
            runtime_model_id: fake-a
            pool: p
            gpu:
              required: true
              device_ids: [9]
          - alias: b
            backend: fake
            runtime_model_id: fake-b
            pool: p
            gpu:
              required: true
              device_ids: [8]
        """,
        gpu_devices="0,1",
    )
    mgr = _manager(settings)
    with pytest.raises(GpuUnavailableError):
        await mgr.chat("p", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    await mgr.shutdown()


# --------------------------------------------------------------------------- #
# A model WITHOUT gpu.required behaves exactly as before (no telemetry probe,
# no verification state).
# --------------------------------------------------------------------------- #
async def test_non_required_model_unchanged(tmp_path):
    settings = _settings(
        tmp_path,
        """
        models:
          - alias: plain
            backend: fake
            runtime_model_id: fake-plain
            gpu:
              device_ids: [0]
        """,
    )
    mgr = _manager(settings)
    out = await mgr.chat("plain", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    assert out["choices"][0]["message"]["content"] == "echo: plain"
    assert mgr.models["plain"].gpu_verification is None
    await mgr.shutdown()


async def test_status_exposes_gpu_block(tmp_path):
    settings = _settings(
        tmp_path,
        """
        models:
          - alias: req
            backend: fake
            runtime_model_id: fake-req
            gpu:
              required: true
              device_ids: [0]
        """,
        gpu_devices="0",
    )
    mgr = _manager(settings)
    await mgr.chat("req", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    st = await mgr.status()
    gpu = next(mm["gpu"] for mm in st["models"] if mm["model"] == "req")
    assert gpu["required"] is True
    assert gpu["device_ids"] == [0]
    assert gpu["evidence"] == "measured"
    assert gpu["verification"]["inventory_source"] == "declared"
    await mgr.shutdown()
