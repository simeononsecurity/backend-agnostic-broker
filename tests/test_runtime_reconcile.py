"""Restart-reconciliation integration tests with REAL orphan processes (ADR 0021).

Each test spawns a genuine child HTTP server (an argv-only ``python -m
http.server`` standing in for a runtime), records it in the ledger as though a
previous broker had spawned it, drops the broker's in-memory state, then runs a
fresh manager's startup reconciliation and asserts adopt / stop / report / stale
behave exactly as specified. No GPU and no real model are involved.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from broker import runtime_identity as rid
from broker.config import Settings
from broker.errors import ModelLoadError
from broker.ledger import RuntimeRecord, argv_hash
from broker.lifecycle import State
from broker.manager import ModelManager
from broker.registry import load_registry


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def orphans():
    procs: list[subprocess.Popen] = []
    yield procs
    for p in procs:
        try:
            os.killpg(os.getpgid(p.pid), 9)
        except (ProcessLookupError, OSError):
            pass
        try:
            p.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass


def _spawn_http_orphan(port: int, procs: list) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        start_new_session=True,  # own session/process-group, like a real runtime
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


def _record_for(manager: ModelManager, proc: subprocess.Popen, port: int, *, start_time=None):
    mm = manager.get("r1")
    st = start_time if start_time is not None else rid.process_start_time(proc.pid)
    return RuntimeRecord(
        alias="r1",
        pid=proc.pid,
        pgid=rid.pgid_of(proc.pid),
        start_time=st,
        port=port,
        argv_sha256=argv_hash(mm.adapter.planned_argv()),
        broker_instance_id="previous-broker",
        base_url=f"http://127.0.0.1:{port}",
        spawned_at=time.time(),
    )


async def test_reconcile_adopt_marks_ready_without_spawning(tmp_path, orphans):
    port = _free_port()
    proc = _spawn_http_orphan(port, orphans)
    mgr = _build_manager(tmp_path, port, "adopt")
    mgr.ledger.upsert(_record_for(mgr, proc, port))

    summary = await mgr.reconcile()

    assert summary["adopted"] == ["r1"]
    mm = mgr.get("r1")
    assert mm.machine.state == State.READY
    assert mm.adopted is True
    assert mm.machine.pid == proc.pid
    assert mgr.loaded_count() == 1
    # The orphan was NOT killed and NO new process was spawned (adopted in place).
    assert rid.pid_alive(proc.pid)
    # Graceful shutdown stops the adopted runtime AND clears the ledger.
    await mgr.shutdown()
    assert mgr.ledger.load() == []
    time.sleep(0.2)
    assert rid.process_is_zombie(proc.pid) or not rid.pid_alive(proc.pid)


async def test_reconcile_stop_terminates_and_clears(tmp_path, orphans):
    port = _free_port()
    proc = _spawn_http_orphan(port, orphans)
    mgr = _build_manager(tmp_path, port, "stop")
    mgr.ledger.upsert(_record_for(mgr, proc, port))

    summary = await mgr.reconcile()

    assert len(summary["stopped"]) == 1
    assert summary["stopped"][0]["alias"] == "r1"
    assert summary["stopped"][0]["status"] in ("terminated", "killed")
    assert mgr.ledger.load() == []  # record cleared
    assert mgr.get("r1").machine.state == State.UNLOADED  # nothing loaded
    time.sleep(0.2)
    assert rid.process_is_zombie(proc.pid) or not rid.pid_alive(proc.pid)
    await mgr.shutdown()


async def test_reconcile_report_lists_orphan_without_signalling(tmp_path, orphans):
    port = _free_port()
    proc = _spawn_http_orphan(port, orphans)
    mgr = _build_manager(tmp_path, port, "report")
    mgr.ledger.upsert(_record_for(mgr, proc, port))

    summary = await mgr.reconcile()

    assert len(summary["orphaned"]) == 1
    entry = summary["orphaned"][0]
    assert entry["alias"] == "r1" and entry["pid"] == proc.pid and entry["port"] == port
    # Report never signals and never adopts: process alive, model not loaded,
    # and the record is kept so it keeps showing until resolved.
    assert rid.pid_alive(proc.pid)
    assert mgr.get("r1").machine.state == State.UNLOADED
    assert {r.alias for r in mgr.ledger.load()} == {"r1"}
    await mgr.shutdown()


async def test_pid_reuse_is_never_signalled(tmp_path, orphans):
    """A record whose start time does not match is stale — never signalled."""
    port = _free_port()
    proc = _spawn_http_orphan(port, orphans)
    mgr = _build_manager(tmp_path, port, "stop")
    # Wrong start time simulates the PID having been reused by an unrelated process.
    mgr.ledger.upsert(_record_for(mgr, proc, port, start_time="0.01"))

    summary = await mgr.reconcile()

    assert summary["stopped"] == []
    assert {s["alias"] for s in summary["stale"]} == {"r1"}
    # The live process was NOT killed (its identity didn't match the record)...
    assert rid.pid_alive(proc.pid)
    assert not rid.process_is_zombie(proc.pid)
    # ...and the stale record was dropped.
    assert mgr.ledger.load() == []
    await mgr.shutdown()


async def test_corrupt_ledger_is_ignored_never_kills(tmp_path, orphans):
    port = _free_port()
    proc = _spawn_http_orphan(port, orphans)
    mgr = _build_manager(tmp_path, port, "stop")
    # A torn ledger must never cause a kill (fail-safe).
    Path(mgr._ledger_path(mgr.settings)).write_text("{ not valid json ]")

    summary = await mgr.reconcile()

    assert summary["checked"] == 0
    assert summary["stopped"] == [] and summary["stale"] == []
    assert rid.pid_alive(proc.pid) and not rid.process_is_zombie(proc.pid)
    await mgr.shutdown()


async def test_port_precheck_fails_fast_when_orphan_holds_port(tmp_path, orphans):
    port = _free_port()
    _spawn_http_orphan(port, orphans)
    mgr = _build_manager(tmp_path, port, "off")  # off: no adopt/stop, just spawn
    mm = mgr.get("r1")
    with pytest.raises(ModelLoadError) as exc:
        await mm.adapter.start()
    assert str(port) in str(exc.value)
    await mgr.shutdown()


async def test_adopt_refused_on_argv_drift_lists_as_orphan(tmp_path, orphans):
    port = _free_port()
    proc = _spawn_http_orphan(port, orphans)
    mgr = _build_manager(tmp_path, port, "adopt")
    rec = _record_for(mgr, proc, port)
    rec.argv_sha256 = argv_hash(["/usr/bin/something", "else"])  # registry drifted
    mgr.ledger.upsert(rec)

    summary = await mgr.reconcile()

    assert summary["adopted"] == []
    assert len(summary["orphaned"]) == 1
    assert summary["orphaned"][0]["adoptable"] is False
    assert mgr.get("r1").machine.state == State.UNLOADED
    assert rid.pid_alive(proc.pid)  # never signalled
    await mgr.shutdown()


def test_python_m_broker_handles_sigterm_gracefully(tmp_path):
    """`python -m broker` must run a GRACEFUL shutdown on SIGTERM (PID 1 / Docker).

    uvicorn installs a SIGTERM handler that drives the FastAPI lifespan shutdown
    (which stops runtimes and clears the ledger). We assert the lifespan shutdown
    actually ran — the invariant that prevents orphaned runtimes on `docker stop`
    — and that the process terminated promptly rather than hanging. (uvicorn's own
    post-shutdown exit status on SIGTERM is 143/-15 by convention, which Docker
    treats as a normal stop; what matters here is that cleanup ran.)
    """
    import signal

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
    port = _free_port()
    env = dict(os.environ)
    env.update(
        BROKER_AUTH_ENABLED="false",
        BROKER_REGISTRY_PATH=str(reg_file),
        BROKER_CACHE_DIR=str(tmp_path),
        BROKER_PORT=str(port),
        BROKER_HOST="127.0.0.1",
        BROKER_USAGE_LOG_ENABLED="false",
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "broker"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        ready = False
        while time.monotonic() < deadline:
            if rid.port_in_use("127.0.0.1", port):
                ready = True
                break
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        assert ready, "broker did not start listening"
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)  # must terminate promptly, not hang
        output = proc.stdout.read() if proc.stdout else ""
        # The lifespan shutdown (manager.shutdown → stop runtimes + clear ledger)
        # ran in response to SIGTERM.
        assert "Application shutdown complete" in output, output
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
