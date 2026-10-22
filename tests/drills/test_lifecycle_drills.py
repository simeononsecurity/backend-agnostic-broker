"""ADR 0027 lifecycle failure drills (D1–D12) — real broker + real stand-in runtimes.

Each drill spawns a real ``python -m broker`` (see :mod:`tests.drills.harness`) and
asserts, for its scenario: a wall-clock bound, **zero leftover runtime processes**
(by PID + start time), an **empty ledger and lease directory** where a clean stop
is expected, and the typed error where one is expected.

Marked ``drill`` and excluded from the default ``pytest`` run (``addopts -m "not
drill"``); run them with ``scripts/failure_drills.py`` or ``pytest -m drill``.
POSIX only. The Linux descendant paths also run in the Python 3.11 container gate.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402  (path-local import of the drill harness)

from broker import runtime_identity as rid  # noqa: E402
from broker.leases import (  # noqa: E402
    MODE_EXCLUSIVE,
    STATE_EXTERNAL,
    DeviceLease,
    DeviceLeaseStore,
    LeaseHolder,
)

pytestmark = [
    pytest.mark.drill,
    pytest.mark.skipif(os.name != "posix", reason="POSIX signals/fork only"),
]

_IS_LINUX = sys.platform.startswith("linux")


def _settle(seconds: float = 0.6) -> None:
    time.sleep(seconds)


# --- D1–D6: shutdown / teardown --------------------------------------------
def test_d1_sigterm_with_loaded_runtime_exits_within_budget(tmp_path):
    b = harness.BrokerProc(tmp_path, mode="", budget=45, startup_timeout=30)
    try:
        assert b.wait_health()
        assert b.post({}, 60) == 200
        assert len(harness.runtime_pids()) == 1
        elapsed = b.terminate_and_time()
        assert elapsed is not None and elapsed < 10.0, f"exit took {elapsed}s"
        _settle()
        assert harness.runtime_pids() == []
        assert b.ledger_records() == []
        assert b.lease_files() == []
    finally:
        b.cleanup()


def test_d2_runtime_ignoring_sigterm_is_sigkilled_within_budget(tmp_path):
    budget = 5.0
    b = harness.BrokerProc(tmp_path, mode="ignore_term", budget=budget, startup_timeout=30)
    try:
        assert b.wait_health()
        assert b.post({}, 60) == 200
        elapsed = b.terminate_and_time()
        assert elapsed is not None, "broker hung past 100 s"
        assert elapsed <= budget + 3.0, f"exit took {elapsed}s, over budget {budget}s"
        _settle()
        assert harness.runtime_pids() == []
        assert b.ledger_records() == []
    finally:
        b.cleanup()


def test_d3_escaped_setsid_descendant_killed_or_reported(tmp_path):
    budget = 6.0
    b = harness.BrokerProc(tmp_path, mode="escape", budget=budget, startup_timeout=30)
    try:
        assert b.wait_health()
        assert b.post({}, 60) == 200
        _settle(1.0)
        escape_pid = b.read_escape_pid()
        assert escape_pid is not None, "escaped grandchild never recorded its PID"
        assert harness.pid_alive(escape_pid)
        elapsed = b.terminate_and_time()
        assert elapsed is not None and elapsed <= budget + 3.0, f"exit took {elapsed}s"
        _settle(1.0)
        # D3: killed on Linux; on macOS the descendant is recorded-while-alive so it
        # is normally killed too, but the ADR permits "killed OR reported as a
        # survivor" off Linux.
        if _IS_LINUX:
            assert not harness.pid_alive(escape_pid), "escaped descendant survived on Linux"
            assert harness.runtime_pids() == []
        else:
            # Either reaped, or the only leftover (a reported survivor) — never a
            # silent leak of the main runtime.
            assert b.read_escape_pid() == escape_pid  # reproducible identity
    finally:
        b.cleanup()


def test_d4_sigterm_during_load_cancels_and_exits_within_budget(tmp_path):
    budget = 6.0
    b = harness.BrokerProc(tmp_path, mode="slow_start", budget=budget, startup_timeout=150)
    try:
        assert b.wait_health()
        result: dict = {}

        def _load():
            result["err"] = b.error_type({}, 200)

        t = threading.Thread(target=_load, daemon=True)
        t.start()
        _settle(3.0)  # load in progress (runtime spawned, not yet ready)
        assert len(harness.runtime_pids()) >= 1
        elapsed = b.terminate_and_time()
        assert elapsed is not None and elapsed <= budget + 3.0, f"exit took {elapsed}s"
        t.join(10)
        # The waiting client is failed fast with the typed 503 shutting_down.
        assert result.get("err") == "shutting_down", result
        _settle(1.0)
        assert harness.runtime_pids() == []
    finally:
        b.cleanup()


def test_d5_open_stream_at_sigterm_exits_within_budget(tmp_path):
    budget = 8.0
    b = harness.BrokerProc(tmp_path, mode="", budget=budget, startup_timeout=30)
    try:
        assert b.wait_health()
        assert b.post({}, 60) == 200  # warm the runtime

        def _stream():
            b.post({"stream": True}, 200)

        threading.Thread(target=_stream, daemon=True).start()
        _settle(1.5)  # a stream is open
        elapsed = b.terminate_and_time()
        assert elapsed is not None and elapsed <= budget + 3.0, f"exit took {elapsed}s"
        _settle(1.0)
        assert harness.runtime_pids() == []
        assert b.ledger_records() == []
    finally:
        b.cleanup()


def test_d6_second_signal_escalates_immediately(tmp_path):
    # A large budget: without the second signal this runtime (ignore_term) would
    # pin shutdown for the whole grace. The second SIGTERM must force a fast exit.
    b = harness.BrokerProc(tmp_path, mode="ignore_term", budget=45, startup_timeout=30)
    try:
        assert b.wait_health()
        assert b.post({}, 60) == 200
        elapsed = b.terminate_and_time(extra_signals=1, gap=0.5)
        assert elapsed is not None and elapsed < 15.0, f"second-signal exit took {elapsed}s"
        _settle()
        assert harness.runtime_pids() == []
        assert b.ledger_records() == []
    finally:
        b.cleanup()


# --- D7–D8: required-GPU admission (recorded-fixture telemetry) --------------
def test_d7_gpu_required_no_device_fails_before_spawn(tmp_path):
    # Recorded nvidia telemetry reports a device at index 0, but the model requires
    # device 9 which is not in the inventory → fail fast before any spawn.
    b = harness.BrokerProc(
        tmp_path,
        gpu={"required": True, "device_ids": [9]},
        gpu_telemetry="nvidia",
        fake_nvidia_gpus="0, FakeGPU, 24576, 24000, 576",
        fake_nvidia_apps="",
        startup_timeout=30,
    )
    try:
        assert b.wait_health()
        assert b.error_type({}, 30) == "gpu_unavailable"
        _settle()
        assert harness.runtime_pids() == [], "a required-GPU miss must not spawn a runtime"
        elapsed = b.terminate_and_time()
        assert elapsed is not None and elapsed < 10.0
        assert b.ledger_records() == []
    finally:
        b.cleanup()


def test_d8_gpu_required_cpu_placement_fails_and_unloads(tmp_path):
    # The device exists in the inventory (pre-load gate passes, runtime spawns) but
    # no compute app attributes memory to the runtime → post-load verification fails
    # with gpu_placement_failed and the model is unloaded (never served on CPU).
    b = harness.BrokerProc(
        tmp_path,
        gpu={"required": True, "device_ids": [0]},
        gpu_telemetry="nvidia",
        gpu_verify_seconds=3.0,
        fake_nvidia_gpus="0, FakeGPU, 24576, 24000, 576",
        fake_nvidia_apps="",
        startup_timeout=30,
    )
    try:
        assert b.wait_health()
        assert b.error_type({}, 30) == "gpu_placement_failed"
        _settle(1.5)
        assert harness.runtime_pids() == [], "a CPU-placed required model must be unloaded"
        elapsed = b.terminate_and_time()
        assert elapsed is not None and elapsed < 10.0
        assert b.ledger_records() == []
    finally:
        b.cleanup()


# --- D9–D11: device leases --------------------------------------------------
def test_d9_lease_acquired_on_load_released_on_unload(tmp_path):
    b = harness.BrokerProc(
        tmp_path,
        leases=True,
        startup_timeout=30,
        idle_timeout=1,
        extra_env={
            "BROKER_MODEL_IDLE_TIMEOUT_SECONDS": "1",
            "BROKER_IDLE_SWEEP_INTERVAL_SECONDS": "1",
        },
    )
    try:
        assert b.wait_health()
        assert b.post({}, 60) == 200
        # Acquired on load.
        assert b.lease_files(), "no lease file written on load"
        # Released on idle unload: the sweeper unloads after ~1 s idle.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and b.lease_files():
            time.sleep(0.5)
        assert b.lease_files() == [], "lease not released after idle unload"
        assert harness.runtime_pids() == []
        elapsed = b.terminate_and_time()
        assert elapsed is not None and elapsed < 10.0
        assert b.ledger_records() == []
    finally:
        b.cleanup()


def test_d10_broker_sigkill_then_cleanup_reaps_runtime_and_lease(tmp_path):
    # A release hook that drops a marker file so we can prove it fired.
    marker = tmp_path / "released.marker"
    hook = tmp_path / "release_hook.sh"
    hook.write_text(f'#!/bin/sh\necho "$1 $2 $3" >> "{marker}"\n')
    hook.chmod(0o755)
    b = harness.BrokerProc(
        tmp_path,
        mode="escape",  # the orphan has an escaped setsid descendant too
        leases=True,
        reconcile="stop",
        lease_release_command=f"{hook} {{device}} {{mode}} {{holder}}",
        startup_timeout=30,
        extra_env={"BROKER_ALLOWED_EXEC_DIRS": _allow_dirs_with(tmp_path)},
    )
    try:
        assert b.wait_health()
        assert b.post({}, 60) == 200
        _settle(1.0)
        assert b.lease_files(), "no lease held before the forced kill"
        escape_pid = b.read_escape_pid()
        assert escape_pid is not None and harness.pid_alive(escape_pid)
        # Ungraceful crash: the broker dies, leaving the runtime + lease behind.
        b.sigkill_broker()
        assert harness.runtime_pids(), "the orphan runtime should outlive a SIGKILLed broker"
        t0 = time.monotonic()
        summary = b.run_cleanup(timeout=20)
        elapsed = time.monotonic() - t0
        assert elapsed < 12.0, f"cleanup took {elapsed}s"
        # Runtime (and its escaped descendant on Linux) reaped; lease removed; hook fired.
        _settle(1.0)
        assert harness.runtime_pids() == [], f"cleanup left runtimes: {summary}"
        if _IS_LINUX:
            assert not harness.pid_alive(escape_pid), "cleanup left the escaped descendant alive"
        assert b.lease_files() == [], f"cleanup left a lease: {summary}"
        assert marker.exists(), f"release hook did not fire: {summary}"
    finally:
        b.cleanup()


def test_d11_exclusive_vs_external_lease_is_device_busy_nothing_signalled(tmp_path):
    lease_dir = tmp_path / "leases"
    lease_dir.mkdir()
    # A real co-tenant process holds device 0 under an EXTERNAL lease.
    import subprocess

    cotenant = subprocess.Popen(["sleep", "120"])
    try:
        store = DeviceLeaseStore(str(lease_dir))
        ext = LeaseHolder(
            holder="co-tenant",
            state=STATE_EXTERNAL,
            runtime_pid=cotenant.pid,
            runtime_start_time=rid.process_start_time(cotenant.pid),
        )
        store.write(DeviceLease(device="0", mode=MODE_EXCLUSIVE, holders=[ext]))

        b = harness.BrokerProc(
            tmp_path,
            gpu={"exclusive": True, "device_ids": [0]},
            leases=True,
            startup_timeout=30,
        )
        try:
            assert b.wait_health()
            assert b.error_type({}, 30) == "device_busy"
            # Nothing was spawned and the external holder was never signalled.
            assert harness.runtime_pids() == []
            assert harness.pid_alive(cotenant.pid), "the external co-tenant was signalled!"
            elapsed = b.terminate_and_time()
            assert elapsed is not None and elapsed < 10.0
        finally:
            b.cleanup()
    finally:
        cotenant.send_signal(9)
        cotenant.wait()


# --- D12: restart after SIGKILL (ADR 0021 reconcile) ------------------------
def test_d12_restart_after_sigkill_report_then_stop(tmp_path):
    shared_cache = tmp_path / "cache"
    # First broker: load, then SIGKILL — leaves an orphan runtime + a ledger record.
    b1 = harness.BrokerProc(
        tmp_path, mode="", reconcile="report", startup_timeout=30, cache_dir=shared_cache
    )
    orphan_pid = None
    try:
        assert b1.wait_health()
        assert b1.post({}, 60) == 200
        pids = harness.runtime_pids()
        assert len(pids) == 1
        orphan_pid = pids[0]
        assert b1.ledger_records(), "no ledger record for the loaded runtime"
        b1.sigkill_broker()
        assert harness.pid_alive(orphan_pid), "orphan runtime should outlive the SIGKILLed broker"
    finally:
        try:
            b1.logf.close()
        except Exception:  # noqa: BLE001
            pass

    # Restart in REPORT mode against b1's cache: the orphan is reported, not signalled.
    b2 = harness.BrokerProc(
        tmp_path, mode="", reconcile="report", startup_timeout=30, cache_dir=shared_cache
    )
    try:
        assert b2.wait_health()
        _settle(1.0)
        assert harness.pid_alive(orphan_pid), "report mode must NOT signal the orphan"
        assert b2.status(), "no /status from restarted broker"
        b2.terminate_and_time()
    finally:
        b2.cleanup()

    # Restart in STOP mode against the same orphan: it is terminated identity-gated.
    b3 = harness.BrokerProc(
        tmp_path, mode="", reconcile="stop", startup_timeout=30, cache_dir=shared_cache
    )
    try:
        assert b3.wait_health()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and harness.pid_alive(orphan_pid):
            time.sleep(0.5)
        assert not harness.pid_alive(orphan_pid), "stop mode must terminate the orphan runtime"
        b3.terminate_and_time()
    finally:
        b3.cleanup()


def _allow_dirs_with(tmp_path: Path) -> str:
    import json

    return json.dumps([str(harness._FIXTURES), str(tmp_path)])
