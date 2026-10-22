"""Unit + focused tests for ADR 0024 teardown primitives.

Covers the identity gate, descendant enumeration, the subreaper best-effort
call, and that an adopted-runtime stop runs off the event loop (a concurrent
coroutine stays responsive while the grace elapses).
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time

import pytest

from broker import runtime_identity as rid
from broker.adapters import make_adapter
from broker.config import Settings
from broker.registry import BackendType, ModelEntry
from broker.teardown import (
    TeardownResult,
    broker_zombie_pids,
    enumerate_descendants,
    reap_broker_zombies,
    reap_reparented,
    set_child_subreaper,
    terminate_tree,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX signals/fork only")


def _spawn(argv, *, new_session=True):
    return subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=new_session,
    )


def test_teardown_result_ok_and_dict():
    r = TeardownResult(alias="m", outcome="signalled", exited=[2, 1])
    assert r.ok()
    d = r.to_dict()
    assert d["outcome"] == "signalled"
    assert d["exited"] == [1, 2]  # sorted
    assert TeardownResult(outcome="survivors", survivors=[9]).ok() is False


def test_set_child_subreaper_is_best_effort():
    rc = set_child_subreaper()
    # True only on Linux where prctl succeeds; never raises on any platform.
    assert rc in (True, False)
    if not sys.platform.startswith("linux"):
        assert rc is False


def test_enumerate_descendants_finds_children():
    # A shell that forks a child `sleep` and waits: the child is a descendant.
    proc = _spawn(["/bin/sh", "-c", "sleep 30 & wait"])
    try:
        time.sleep(0.5)
        desc = enumerate_descendants(proc.pid)
        assert desc, "expected at least the child sleep"
        # Every reported descendant is really alive.
        assert all(rid.pid_alive(p) for p in desc)
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()


def test_terminate_tree_signals_and_kills_a_real_group():
    proc = _spawn(["/bin/sleep", "30"])
    pid = proc.pid
    start_time = rid.process_start_time(pid)
    pgid = os.getpgid(pid)
    result = terminate_tree(
        main_pid=pid,
        main_start_time=start_time,
        pgid=pgid,
        grace=3.0,
        alias="m",
        backend="test",
    )
    assert result.ok()
    assert result.outcome in ("signalled", "killed")
    assert pid in result.exited
    assert not rid.pid_alive(pid) or rid.process_is_zombie(pid)
    proc.wait()


def test_terminate_tree_refuses_on_identity_mismatch():
    proc = _spawn(["/bin/sleep", "30"])
    pid = proc.pid
    try:
        result = terminate_tree(
            main_pid=pid,
            main_start_time="not-the-real-start-time",
            pgid=os.getpgid(pid),
            grace=1.0,
            alias="m",
            backend="test",
        )
        assert result.outcome == "identity_mismatch"
        assert rid.pid_alive(pid)  # never signalled a non-matching PID
    finally:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()


async def test_adopted_stop_does_not_block_the_event_loop():
    settings = Settings(auth_enabled=False)
    entry = ModelEntry(
        alias="m",
        backend=BackendType.LLAMACPP,
        runtime_model_id="m",
        base_url="http://127.0.0.1:18799",
        model_path="/tmp/does-not-matter.gguf",
        startup_command=["/bin/sleep", "30"],
    )
    adapter = make_adapter(entry, settings)
    # An adopted runtime that ignores SIGTERM, so terminate_tree spends the full
    # grace before SIGKILL — if that ran on the loop, the probe below would stall.
    proc = _spawn(["/bin/sh", "-c", 'trap "" TERM; sleep 30'])
    pid = proc.pid
    adapter.mark_adopted(pid, os.getpgid(pid), rid.process_start_time(pid))

    ticks: list[float] = []

    async def probe():
        try:
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            pass

    probe_task = asyncio.create_task(probe())
    t0 = time.monotonic()
    result = await adapter.stop(grace=1.5)
    duration = time.monotonic() - t0
    probe_task.cancel()
    await probe_task

    try:
        assert result is not None and result.ok()
        assert not rid.pid_alive(pid) or rid.process_is_zombie(pid)
        # The grace actually elapsed (SIGTERM was ignored) ...
        assert duration >= 1.0
        # ... yet the event loop kept ticking throughout (never blocked > 0.3 s).
        gaps = [ticks[i + 1] - ticks[i] for i in range(len(ticks) - 1)]
        assert gaps, "probe never ran"
        assert max(gaps) < 0.3, f"event loop stalled {max(gaps):.3f}s during adopted stop"
    finally:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()


_LINUX = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="subreaper re-parenting + /proc zombie reaping are Linux-only",
)


def test_teardown_result_reaped_in_dict():
    r = TeardownResult(alias="m", outcome="killed", reaped=[7, 3])
    assert r.to_dict()["reaped"] == [3, 7]  # sorted


def test_reap_reparented_is_noop_for_foreign_pid():
    # A no-op on a PID whose parent is not this process (e.g. init): never raises,
    # never reaps something that is not ours.
    assert reap_reparented([1], broker_pid=os.getpid()) == []
    assert reap_reparented([], broker_pid=os.getpid()) == []


@_LINUX
def test_reap_reparented_reaps_a_broker_zombie_child():
    # A direct child that exits without being waited on becomes a zombie child of
    # this process; reap_reparented must waitpid exactly it (ppid == us).
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child
        os._exit(0)
    for _ in range(100):
        if rid.process_is_zombie(pid):
            break
        time.sleep(0.01)
    assert pid in broker_zombie_pids(os.getpid())
    # A PID in the skip set is never reaped (it models the asyncio-owned main).
    assert reap_reparented([pid], broker_pid=os.getpid(), skip={pid}) == []
    # Reaping it returns the pid, and a second reap is a harmless no-op (ECHILD).
    assert reap_reparented([pid], broker_pid=os.getpid()) == [pid]
    assert reap_reparented([pid], broker_pid=os.getpid()) == []
    assert pid not in broker_zombie_pids(os.getpid())


@_LINUX
def test_reap_broker_zombies_sweeps_all_reparented_children():
    pids = []
    for _ in range(3):
        p = os.fork()
        if p == 0:  # pragma: no cover - child
            os._exit(0)
        pids.append(p)
    for p in pids:
        for _ in range(100):
            if rid.process_is_zombie(p):
                break
            time.sleep(0.01)
    # Skip one (models a live runtime the broker spawned): the sweeper leaves it.
    reaped = reap_broker_zombies(os.getpid(), skip={pids[0]})
    # Every non-skipped forked child was reaped (the sweep may also clean up other
    # leaked zombie children of this process, so assert a subset, not equality).
    assert set(pids[1:]) <= set(reaped)
    assert pids[0] not in reaped
    assert pids[0] in broker_zombie_pids(os.getpid())
    # Clean up the skipped one so the test process leaves no zombie behind.
    os.waitpid(pids[0], 0)
