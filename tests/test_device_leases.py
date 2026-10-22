"""ADR 0026: host-local device leases — exclusive admission + lease lifecycle.

Fake backend + a real :class:`~broker.manager.ModelManager`: a load writes a
``held`` lease and fires the acquire hook once; an unload removes it and fires
the release hook once; an exclusive model evicts an idle shared holder and is
refused on a device held by a busy model or an external holder (whose real
process is never signalled); stale leases are reaped. Leases OFF create no dir.
"""

from __future__ import annotations

import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from broker import runtime_identity as rid
from broker.config import Settings
from broker.errors import DeviceBusyError
from broker.leases import (
    MODE_EXCLUSIVE,
    MODE_SHARED,
    STATE_EXTERNAL,
    STATE_HELD,
    DeviceLease,
    LeaseHolder,
    holder_is_orphan,
    holder_is_stale,
    incompatible_holders,
    validate_hook_command,
)
from broker.lifecycle import State
from broker.manager import ModelManager
from broker.registry import load_registry

HOOK = Path(__file__).parent / "fixtures" / "lease_hook.sh"


def _settings(tmp_path: Path, registry_yaml: str, **over) -> Settings:
    reg = tmp_path / "registry.yaml"
    reg.write_text(textwrap.dedent(registry_yaml).strip())
    kw = dict(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(reg),
        enabled_backends=["fake", "ollama", "vllm", "llamacpp", "strata"],
        allowed_model_roots=[str(tmp_path)],
        allowed_exec_dirs=[str(HOOK.parent)],
        cache_dir=str(tmp_path / "cache"),
        model_idle_timeout_seconds=60,
        idle_sweep_interval_seconds=5,
        max_loaded_models=2,
        device_leases="on",
        device_lease_dir=str(tmp_path / "leases"),
    )
    kw.update(over)
    return Settings(**kw)


def _manager(settings: Settings) -> ModelManager:
    reg = load_registry(settings.registry_path, set(settings.enabled_backends))
    mgr = ModelManager(reg, settings)
    mgr.validate_all()
    return mgr


async def _chat(mgr: ModelManager, alias: str, rid_: str = "r1") -> dict:
    return await mgr.chat(alias, {"messages": [{"role": "user", "content": "x"}]}, rid_)


# --------------------------------------------------------------------------- #
# Pure classification / compatibility (no manager).
# --------------------------------------------------------------------------- #
def test_incompatible_holders_matrix():
    shared_lease = DeviceLease(device="0", mode=MODE_SHARED, holders=[LeaseHolder(holder="a")])
    excl_lease = DeviceLease(device="0", mode=MODE_EXCLUSIVE, holders=[LeaseHolder(holder="x")])
    # exclusive request conflicts with any holder; shared+shared is compatible;
    # shared request conflicts only with an exclusive holder.
    assert incompatible_holders(shared_lease, MODE_EXCLUSIVE) == shared_lease.holders
    assert incompatible_holders(shared_lease, MODE_SHARED) == []
    assert incompatible_holders(excl_lease, MODE_SHARED) == excl_lease.holders
    assert incompatible_holders(None, MODE_EXCLUSIVE) == []


def test_stale_and_orphan_classification():
    dead = 2_000_111  # a PID that is not alive
    stale = LeaseHolder(holder="a", broker_pid=dead, broker_start_time="x", runtime_pid=dead)
    assert holder_is_stale(stale)
    assert not holder_is_orphan(stale)
    # External holders are never stale/orphan (never acted on).
    ext = LeaseHolder(holder="co", state=STATE_EXTERNAL, runtime_pid=dead)
    assert not holder_is_stale(ext)
    assert not holder_is_orphan(ext)


# --------------------------------------------------------------------------- #
# Leases OFF: no dir, exact previous behaviour.
# --------------------------------------------------------------------------- #
async def test_leases_off_creates_no_dir(tmp_path):
    settings = _settings(
        tmp_path,
        """
        models:
          - alias: m
            backend: fake
            runtime_model_id: fake-m
            gpu:
              device_ids: [0]
        """,
        device_leases="off",
        device_lease_dir=str(tmp_path / "leases"),
    )
    mgr = _manager(settings)
    try:
        assert not mgr.leases.enabled
        out = await _chat(mgr, "m")
        assert out["choices"][0]["message"]["content"] == "echo: m"
        await mgr.models["m"].unload()
        assert not (tmp_path / "leases").exists()
        assert mgr._leases_block() == {"enabled": False}
    finally:
        await mgr.shutdown()


# --------------------------------------------------------------------------- #
# Load writes a held lease + acquire hook; unload removes it + release hook.
# --------------------------------------------------------------------------- #
async def test_load_writes_lease_unload_removes_it_with_hooks(tmp_path):
    acq = tmp_path / "acquire.log"
    rel = tmp_path / "release.log"
    settings = _settings(
        tmp_path,
        """
        models:
          - alias: m
            backend: fake
            runtime_model_id: fake-m
            gpu:
              device_ids: [0]
        """,
        lease_acquire_command=f"{HOOK} {acq} {{device}} {{mode}} {{holder}}",
        lease_release_command=f"{HOOK} {rel} {{device}} {{mode}} {{holder}}",
    )
    mgr = _manager(settings)
    try:
        await _chat(mgr, "m")
        lease = mgr.leases.read("0")
        assert lease is not None
        assert lease.mode == MODE_SHARED
        assert lease.primary.holder == "m"
        assert lease.primary.state == STATE_HELD
        # The runtime identity was stamped on the cold path.
        assert lease.primary.runtime_pid is not None
        assert acq.read_text().strip() == "0 shared m"
        # Unload → lease file removed, release hook fired exactly once.
        await mgr.models["m"].unload()
        assert mgr.leases.read("0") is None
        assert rel.read_text().strip() == "0 shared m"
        # Acquire hook fired exactly once (one line).
        assert acq.read_text().count("\n") == 1
    finally:
        await mgr.shutdown()


# --------------------------------------------------------------------------- #
# Exclusive admission: evict an idle shared holder, refuse a busy one.
# --------------------------------------------------------------------------- #
async def test_exclusive_evicts_idle_shared_holder(tmp_path):
    settings = _settings(
        tmp_path,
        """
        models:
          - alias: shared
            backend: fake
            runtime_model_id: fake-shared
            gpu:
              device_ids: [0]
          - alias: excl
            backend: fake
            runtime_model_id: fake-excl
            gpu:
              device_ids: [0]
              exclusive: true
        """,
    )
    mgr = _manager(settings)
    try:
        await _chat(mgr, "shared")
        assert mgr.leases.read("0").mode == MODE_SHARED
        # Exclusive model on the same device evicts the idle shared holder.
        await _chat(mgr, "excl")
        assert mgr.models["shared"].machine.state == State.UNLOADED
        lease = mgr.leases.read("0")
        assert lease.mode == MODE_EXCLUSIVE
        assert [h.holder for h in lease.holders] == ["excl"]
    finally:
        await mgr.shutdown()


async def test_exclusive_refuses_busy_holder(tmp_path):
    settings = _settings(
        tmp_path,
        """
        models:
          - alias: busy
            backend: fake
            runtime_model_id: fake-busy
            gpu:
              device_ids: [0]
          - alias: excl
            backend: fake
            runtime_model_id: fake-excl
            gpu:
              device_ids: [0]
              exclusive: true
        """,
    )
    mgr = _manager(settings)
    try:
        await _chat(mgr, "busy")
        # Simulate an in-flight request so the holder is not idle-evictable.
        mgr.models["busy"].machine.inflight = 1
        with pytest.raises(DeviceBusyError) as ei:
            await _chat(mgr, "excl")
        assert ei.value.http_status == 503
        assert ei.value.error_type == "device_busy"
        assert "Retry-After" in ei.value.headers
        # The busy holder was NOT evicted; the exclusive model did not load.
        assert mgr.models["busy"].machine.state in (State.READY, State.BUSY)
        assert mgr.models["excl"].machine.state == State.UNLOADED
        assert [h.holder for h in mgr.leases.read("0").holders] == ["busy"]
    finally:
        mgr.models["busy"].machine.inflight = 0
        await mgr.shutdown()


# --------------------------------------------------------------------------- #
# External exclusive holder: device_busy, and its real process is never touched.
# --------------------------------------------------------------------------- #
async def test_external_exclusive_lease_blocks_and_is_never_signalled(tmp_path):
    settings = _settings(
        tmp_path,
        """
        models:
          - alias: m
            backend: fake
            runtime_model_id: fake-m
            gpu:
              device_ids: [0]
        """,
    )
    mgr = _manager(settings)
    proc = subprocess.Popen(["sleep", "30"])
    try:
        st = rid.process_start_time(proc.pid)
        external = LeaseHolder(
            holder="cotenant",
            state=STATE_EXTERNAL,
            runtime_pid=proc.pid,
            runtime_start_time=st,
            acquired_at=time.time(),
        )
        mgr.leases.write(DeviceLease(device="0", mode=MODE_EXCLUSIVE, holders=[external]))
        with pytest.raises(DeviceBusyError) as ei:
            await _chat(mgr, "m")
        assert ei.value.http_status == 503
        assert "Retry-After" in ei.value.headers
        # The external holder's process is still alive (never signalled), and its
        # lease is intact.
        assert proc.poll() is None
        lease = mgr.leases.read("0")
        assert [h.holder for h in lease.holders] == ["cotenant"]
        assert mgr.models["m"].machine.state == State.UNLOADED
    finally:
        proc.kill()
        proc.wait()
        await mgr.shutdown()


# --------------------------------------------------------------------------- #
# Stale lease (dead broker + dead runtime) is reaped by cleanup_stale_leases.
# --------------------------------------------------------------------------- #
def test_example_hook_script_runs(tmp_path):
    """The shipped examples/device-leases hook script runs and logs its args."""
    script = (
        Path(__file__).resolve().parents[1] / "examples" / "device-leases" / "pause-cotenant.sh"
    )
    assert script.exists()
    log = tmp_path / "hooklog.txt"
    out = subprocess.run(
        [str(script), "0", "exclusive", "llama3-8b"],
        env={**__import__("os").environ, "LEASE_HOOK_LOG": str(log)},
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    assert log.read_text().strip() == "acquire-or-release device=0 mode=exclusive holder=llama3-8b"


async def test_stale_lease_reaped_by_sweeper(tmp_path):
    settings = _settings(
        tmp_path,
        """
        models:
          - alias: m
            backend: fake
            runtime_model_id: fake-m
            gpu:
              device_ids: [0]
        """,
    )
    mgr = _manager(settings)
    try:
        stale = LeaseHolder(
            holder="gone",
            broker_pid=2_000_111,
            broker_start_time="x",
            runtime_pid=2_000_222,
            runtime_start_time="y",
        )
        mgr.leases.write(DeviceLease(device="0", mode=MODE_SHARED, holders=[stale]))
        assert mgr.leases.read("0") is not None
        summary = await mgr.cleanup_stale_leases()
        assert "0" in summary["removed"]
        assert mgr.leases.read("0") is None
    finally:
        await mgr.shutdown()


# Hook-command validation (ADR 0026, comment 94): a hook is a single argv-template
# string, not a JSON list; a malformed one is warned at startup naming the setting.
def test_validate_hook_command_accepts_valid_single_string(tmp_path):
    hook = tmp_path / "acquire.sh"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)
    cmd = f"{hook} {{device}} {{mode}} {{holder}}"
    assert (
        validate_hook_command(
            cmd, setting="BROKER_LEASE_ACQUIRE_COMMAND", allowed_exec_dirs=[str(tmp_path)]
        )
        is True
    )


def test_validate_hook_command_empty_is_ok(tmp_path):
    assert (
        validate_hook_command(
            None, setting="BROKER_LEASE_ACQUIRE_COMMAND", allowed_exec_dirs=[str(tmp_path)]
        )
        is True
    )


def test_validate_hook_command_json_list_is_warned_naming_setting(tmp_path, caplog):
    # A value written as a JSON list (the mistake that cost a probe rerun): the
    # first token '["/bin/true",' is not an allow-listed executable, so it is
    # rejected and the warning names the setting.
    bad = '["/bin/true", "{device}"]'
    with caplog.at_level("WARNING"):
        ok = validate_hook_command(
            bad, setting="BROKER_LEASE_ACQUIRE_COMMAND", allowed_exec_dirs=[str(tmp_path)]
        )
    assert ok is False
    assert "BROKER_LEASE_ACQUIRE_COMMAND" in caplog.text
