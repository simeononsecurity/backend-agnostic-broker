"""Multi-replica safety for the runtime ledger (ADR 0021 follow-up).

Two mechanisms, defense in depth, so a live sibling broker that accidentally
shares another broker's ``BROKER_CACHE_DIR`` can never have its runtimes stopped
or adopted:

1. **Lifetime ownership lock** — one live broker per cache dir holds an exclusive
   ``flock`` on ``runtime-ledger.owner.lock`` for its whole life. A sibling that
   finds the lock held refuses to start under ``adopt``/``stop`` (fail closed) and
   starts with the ledger disabled under ``report``/``off`` (v0.6.0 compatible).
2. **Per-record owner check** — each ledger record carries its owning broker's PID
   + start time; reconcile acts only on a record whose owner broker is dead, and
   lists a record owned by a live broker as ``owned_by_live_broker``.

Both are exercised with REAL processes (a live stand-in sibling/owner process and
a real orphan runtime), plus a committed two-broker scenario-E test that starts
two genuine ``python -m broker`` instances sharing one cache dir.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
import urllib.request
from pathlib import Path

import pytest

from broker import runtime_identity as rid
from broker.config import Settings
from broker.errors import ConfigError
from broker.ledger import LedgerOwnerLock, LockOwner, RuntimeRecord, argv_hash
from broker.manager import ModelManager
from broker.registry import load_registry


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def procs():
    live: list[subprocess.Popen] = []
    yield live
    for p in live:
        try:
            os.killpg(os.getpgid(p.pid), 9)
        except (ProcessLookupError, OSError):
            try:
                p.kill()
            except (ProcessLookupError, OSError):
                pass
        try:
            p.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass


def _spawn_sleep(procs: list) -> subprocess.Popen:
    """A real, long-lived process standing in for a sibling broker / owner."""
    p = subprocess.Popen(["sleep", "300"], start_new_session=True)
    procs.append(p)
    return p


def _spawn_http_orphan(port: int, procs: list) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    procs.append(proc)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if rid.port_in_use("127.0.0.1", port):
            return proc
        if proc.poll() is not None:
            raise RuntimeError("orphan http.server exited during startup")
        time.sleep(0.05)
    raise RuntimeError("orphan http.server never became ready")


def _build_manager(tmp_path: Path, port: int, mode: str) -> ModelManager:
    reg_text = textwrap.dedent(
        f"""
        models:
          - alias: r1
            backend: vllm
            runtime_model_id: m
            base_url: http://127.0.0.1:{port}
            startup_command: ["{sys.executable}", "-m", "http.server", "{port}"]
            readiness:
              kind: http
              path: /
              expect_status: 200
        """
    ).strip()
    reg_file = tmp_path / "registry.yaml"
    reg_file.write_text(reg_text)
    settings = Settings(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(reg_file),
        enabled_backends=["vllm", "fake"],
        allowed_exec_dirs=[
            os.path.dirname(sys.executable),
            os.path.dirname(os.path.realpath(sys.executable)),
            "/usr/bin",
            "/bin",
        ],
        cache_dir=str(tmp_path),
        health_probe_enabled=False,
        runtime_reconcile=mode,
    )
    registry = load_registry(settings.registry_path, set(settings.enabled_backends))
    return ModelManager(registry, settings)


# --- 1. ownership lock (unit) -------------------------------------------------


def test_owner_lock_exclusive_and_reports_holder(tmp_path):
    path = str(tmp_path / "runtime-ledger.owner.lock")
    a = LedgerOwnerLock(path)
    b = LedgerOwnerLock(path)
    owner_a = LockOwner(pid=1111, start_time="100.0", instance_id="A", acquired_at=1.0)

    assert a.acquire(owner_a) is None  # A gets it
    assert a.held is True

    held_by = b.acquire(LockOwner(pid=2222, start_time="200.0", instance_id="B"))
    assert held_by is not None  # B is refused
    assert held_by.pid == 1111 and held_by.instance_id == "A"
    assert b.held is False

    # Release by A frees it; B can now take it (stale/handover path).
    a.release()
    assert b.acquire(LockOwner(pid=2222, start_time="200.0", instance_id="B")) is None
    assert b.held is True
    b.release()


def test_owner_lock_no_path_is_noop():
    lk = LedgerOwnerLock(None)
    assert lk.acquire(LockOwner(pid=1, start_time=None, instance_id="x")) is None
    assert lk.held is False
    lk.release()  # idempotent, no crash


# --- 1. ownership refusal / disable (integration, real sibling process) -------


def test_acquire_refuses_to_start_under_stop_when_sibling_owns(tmp_path, procs):
    port = _free_port()
    # Sibling broker A owns the ledger first.
    a = _build_manager(tmp_path, port, "report")
    a.acquire_ledger_ownership()
    assert a._owner_lock.held is True

    # Broker B, same cache dir, stop mode → must refuse to start.
    b = _build_manager(tmp_path, port, "stop")
    with pytest.raises(ConfigError) as exc:
        b.acquire_ledger_ownership()
    msg = str(exc.value)
    assert str(tmp_path) in msg  # names the cache dir
    assert "another broker" in msg
    assert b.ledger is not None  # B never disabled its ledger; it bailed out
    a._owner_lock.release()


def test_acquire_refuses_to_start_under_adopt_when_sibling_owns(tmp_path, procs):
    port = _free_port()
    a = _build_manager(tmp_path, port, "report")
    a.acquire_ledger_ownership()
    b = _build_manager(tmp_path, port, "adopt")
    with pytest.raises(ConfigError):
        b.acquire_ledger_ownership()
    a._owner_lock.release()


async def test_acquire_disables_ledger_under_report_when_sibling_owns(tmp_path, procs):
    port = _free_port()
    a = _build_manager(tmp_path, port, "report")
    a.acquire_ledger_ownership()

    b = _build_manager(tmp_path, port, "report")
    b.acquire_ledger_ownership()  # must NOT raise — v0.6.0 compat

    assert b.ledger is None  # ledger disabled for this instance
    assert b.reconcile_disabled_reason is not None
    assert str(tmp_path) in b.reconcile_disabled_reason
    assert all(m.ledger is None for m in b.models.values())
    # Reconcile is inert and never writes/reconciles the sibling's ledger.
    summary = await b.reconcile()
    assert summary["checked"] == 0
    # /status surfaces why.
    st = await b.status()
    assert st["reconciliation"]["disabled_reason"] is not None
    assert st["reconciliation"]["ledger_owned"] is False
    a._owner_lock.release()


async def test_acquire_takes_over_stale_lock(tmp_path):
    """A lock from a dead owner is taken over automatically (flock frees on exit)."""
    port = _free_port()
    a = _build_manager(tmp_path, port, "stop")
    a.acquire_ledger_ownership()
    assert a._owner_lock.held
    a._owner_lock.release()  # simulate A's death: fd closed, flock freed

    b = _build_manager(tmp_path, port, "stop")
    b.acquire_ledger_ownership()  # must succeed, no ConfigError
    assert b._owner_lock.held
    assert b.ledger is not None
    b._owner_lock.release()


# --- 2. per-record owner check (integration, real processes) ------------------


async def test_record_owned_by_live_broker_is_never_stopped(tmp_path, procs):
    """A record whose OWNER broker is alive is left alone, even in stop mode."""
    port = _free_port()
    orphan = _spawn_http_orphan(port, procs)  # stands in for the sibling's runtime
    owner = _spawn_sleep(procs)  # stands in for the live sibling broker A

    mgr = _build_manager(tmp_path, port, "stop")
    mm = mgr.get("r1")
    rec = RuntimeRecord(
        alias="r1",
        pid=orphan.pid,
        pgid=rid.pgid_of(orphan.pid),
        start_time=rid.process_start_time(orphan.pid),
        port=port,
        argv_sha256=argv_hash(mm.adapter.planned_argv()),
        broker_instance_id="sibling-broker-A",
        base_url=f"http://127.0.0.1:{port}",
        spawned_at=time.time(),
        broker_pid=owner.pid,
        broker_start_time=rid.process_start_time(owner.pid),
    )
    mgr.ledger.upsert(rec)

    summary = await mgr.reconcile()

    assert summary["stopped"] == []
    assert summary["stale"] == []
    assert {e["alias"] for e in summary["owned_by_live_broker"]} == {"r1"}
    # The sibling's runtime was NOT killed and the record was kept intact.
    assert rid.pid_alive(orphan.pid) and not rid.process_is_zombie(orphan.pid)
    assert {r.alias for r in mgr.ledger.load()} == {"r1"}

    # Once the owner broker dies, the record becomes actionable again (stop acts).
    os.killpg(os.getpgid(owner.pid), signal.SIGKILL)
    owner.wait(timeout=5)
    summary2 = await mgr.reconcile()
    assert len(summary2["stopped"]) == 1
    time.sleep(0.2)
    assert rid.process_is_zombie(orphan.pid) or not rid.pid_alive(orphan.pid)
    await mgr.shutdown()


async def test_legacy_record_without_owner_identity_reconciles_normally(tmp_path, procs):
    """A record predating owner tracking (no broker_pid) uses the runtime gate."""
    port = _free_port()
    orphan = _spawn_http_orphan(port, procs)
    mgr = _build_manager(tmp_path, port, "stop")
    mm = mgr.get("r1")
    rec = RuntimeRecord(
        alias="r1",
        pid=orphan.pid,
        pgid=rid.pgid_of(orphan.pid),
        start_time=rid.process_start_time(orphan.pid),
        port=port,
        argv_sha256=argv_hash(mm.adapter.planned_argv()),
        broker_instance_id="previous-broker",
        base_url=f"http://127.0.0.1:{port}",
        spawned_at=time.time(),
        # broker_pid / broker_start_time intentionally None (legacy).
    )
    mgr.ledger.upsert(rec)

    summary = await mgr.reconcile()

    assert summary["owned_by_live_broker"] == []
    assert len(summary["stopped"]) == 1  # acted on as an orphan, as before
    await mgr.shutdown()


# --- committed scenario E: two REAL brokers sharing one cache dir -------------


def _fake_registry(tmp_path: Path) -> Path:
    reg_file = tmp_path / "registry.yaml"
    reg_file.write_text(
        textwrap.dedent(
            """
            models:
              - alias: echo
                backend: fake
                runtime_model_id: fake-echo
            """
        ).strip()
    )
    return reg_file


def _broker_env(reg_file: Path, cache_dir: Path, port: int, mode: str) -> dict:
    env = dict(os.environ)
    env.update(
        BROKER_AUTH_ENABLED="false",
        BROKER_REGISTRY_PATH=str(reg_file),
        BROKER_CACHE_DIR=str(cache_dir),
        BROKER_PORT=str(port),
        BROKER_HOST="127.0.0.1",
        BROKER_USAGE_LOG_ENABLED="false",
        BROKER_RUNTIME_RECONCILE=mode,
    )
    return env


def _wait_listening(port: int, proc: subprocess.Popen, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if rid.port_in_use("127.0.0.1", port):
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.1)
    return False


def test_scenario_e_two_real_brokers_sharing_cache_dir(tmp_path):
    """Scenario E, real processes: a shared cache dir never lets B harm A.

    * A starts in report mode and owns the ledger.
    * B started with stop (and adopt) REFUSES to start (non-zero exit, clear error).
    * B started with report starts fine with its ledger disabled.
    * A stays up and serving throughout.
    """
    reg_file = _fake_registry(tmp_path)
    cache = tmp_path / "cache"
    port_a = _free_port()

    procs: list[subprocess.Popen] = []

    def _start(mode: str, port: int) -> subprocess.Popen:
        p = subprocess.Popen(
            [sys.executable, "-m", "broker"],
            env=_broker_env(reg_file, cache, port, mode),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        procs.append(p)
        return p

    try:
        a = _start("report", port_a)
        assert _wait_listening(port_a, a), "broker A did not start"

        def _assert_refuses(mode: str) -> str:
            p = _start(mode, _free_port())
            # Startup must fail: process exits non-zero and never keeps listening.
            rc = p.wait(timeout=20)
            assert rc != 0, f"broker B ({mode}) should have refused to start"
            out = p.stdout.read() if p.stdout else ""
            assert "another broker" in out and str(cache) in out, out
            return out

        _assert_refuses("stop")
        _assert_refuses("adopt")

        # A is still up and serving after both refusals.
        with urllib.request.urlopen(f"http://127.0.0.1:{port_a}/healthz", timeout=5) as r:
            assert r.status == 200

        # B in report mode starts, with the ledger disabled for that instance.
        port_b = _free_port()
        b = _start("report", port_b)
        assert _wait_listening(port_b, b), "broker B (report) should start"
        with urllib.request.urlopen(f"http://127.0.0.1:{port_b}/status", timeout=5) as r:
            import json

            st = json.loads(r.read())
        assert st["reconciliation"]["disabled_reason"] is not None
        assert st["reconciliation"]["ledger_owned"] is False

        # A unaffected.
        with urllib.request.urlopen(f"http://127.0.0.1:{port_a}/healthz", timeout=5) as r:
            assert r.status == 200
    finally:
        for p in procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(timeout=15)
                except Exception:  # noqa: BLE001
                    p.kill()
                    p.wait(timeout=5)
