"""ADR 0025 real-process test: the required-GPU pre-load gate prevents any spawn.

Uses a REAL broker-managed runtime (a llama.cpp-style entry whose
``startup_command`` is the stand-in executable ``tests/fixtures/gpu_stub_runtime.sh``)
and a REAL :class:`~broker.manager.ModelManager`. A control spawn proves the
stand-in is a genuine process; then a ``gpu.required`` entry with an empty device
inventory must 503 ``gpu_unavailable`` with the runtime never started — verified
by the sentinel file, ``pgrep``, and the runtime ledger.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from broker.config import Settings
from broker.errors import GpuUnavailableError
from broker.manager import ModelManager
from broker.proc import ProcessSupervisor
from broker.registry import load_registry

FIXTURE = Path(__file__).parent / "fixtures" / "gpu_stub_runtime.sh"


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _pgrep(needle: str) -> list[str]:
    """PIDs whose argv mentions ``needle`` (best-effort, macOS + Linux)."""
    try:
        out = subprocess.run(
            ["/usr/bin/pgrep", "-f", needle],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in out.stdout.split() if line.strip()]


async def test_control_stub_is_a_real_process(tmp_path):
    """Sanity: the stand-in runtime really spawns and writes its sentinel."""
    sentinel = tmp_path / "control.sentinel"
    sup = ProcessSupervisor([str(FIXTURE), str(sentinel)], alias="ctl", backend="test")
    pid = await sup.start()
    try:
        assert pid and sup.is_running()
        for _ in range(50):
            if sentinel.exists():
                break
            time.sleep(0.02)
        assert sentinel.exists(), "stand-in runtime did not write its sentinel"
    finally:
        await sup.stop(grace=2.0)
    assert not sup.is_running()


async def test_required_gpu_empty_inventory_never_spawns_runtime(tmp_path):
    sentinel = tmp_path / "req.sentinel"
    port = _free_port()
    reg_path = tmp_path / "registry.yaml"
    reg_path.write_text(
        textwrap.dedent(
            f"""
            models:
              - alias: gpureq
                backend: llamacpp
                runtime_model_id: stub
                base_url: http://127.0.0.1:{port}
                startup_command: ["{FIXTURE}", "{sentinel}"]
                gpu:
                  required: true
            """
        ).strip()
    )
    settings = Settings(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(reg_path),
        enabled_backends=["llamacpp"],
        allowed_exec_dirs=[str(FIXTURE.parent)],
        allowed_model_roots=[str(tmp_path)],
        cache_dir=str(tmp_path / "cache"),
        # Telemetry OFF and no declared inventory => empty inventory.
    )
    reg = load_registry(settings.registry_path, set(settings.enabled_backends))
    mgr = ModelManager(reg, settings)
    mgr.validate_all()
    try:
        with pytest.raises(GpuUnavailableError) as ei:
            await mgr.chat("gpureq", {"messages": [{"role": "user", "content": "x"}]}, "r1")
        assert ei.value.http_status == 503
        assert ei.value.error_type == "gpu_unavailable"

        # 1. The stand-in runtime never ran: no sentinel file.
        assert not sentinel.exists(), "runtime was spawned despite gpu_unavailable"
        # 2. pgrep finds no process for our unique sentinel path.
        assert _pgrep(str(sentinel)) == []
        # 3. The runtime ledger holds no record for the alias (nothing spawned).
        assert mgr.ledger is not None
        aliases = {r.alias for r in mgr.ledger.load()}
        assert "gpureq" not in aliases
        # 4. The model is still UNLOADED.
        from broker.lifecycle import State

        assert mgr.models["gpureq"].machine.state == State.UNLOADED
    finally:
        await mgr.shutdown()
        # Defensive: ensure nothing we started lingers.
        for pid in _pgrep(str(sentinel)):
            try:
                os.kill(int(pid), 9)
            except (ProcessLookupError, ValueError, PermissionError):
                pass
