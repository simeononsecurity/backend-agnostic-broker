"""Real-process shutdown drills for ADR 0024 (bounded, descendant-aware teardown).

These spawn a real ``python -m broker`` with a stand-in runtime
(``tests/fixtures/drill_runtime.py``) and measure how long the broker takes to
exit after ``SIGTERM`` and what survives — the same scenarios the orchestrator
reproduced, turned into regression tests. POSIX only (fork / signals / pgrep).

The Linux-specific descendant assertions (subreaper + ``/proc``) also run inside
the project's Python 3.11 container gate.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX signals/fork only")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "drill_runtime.py"
_IS_LINUX = sys.platform.startswith("linux")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _runtime_pids() -> list[int]:
    """PIDs of live drill-runtime processes (parent + any escaped descendant).

    Uses ``/proc`` on Linux (``python:3.11-slim`` has no ``procps``/``pgrep``) and
    falls back to ``pgrep -f`` elsewhere.
    """
    needle = str(_FIXTURE)
    if _IS_LINUX and os.path.isdir("/proc"):
        pids: list[int] = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as fh:
                    cmd = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
            except OSError:
                continue
            if needle in cmd:
                pids.append(int(entry))
        return pids
    out = subprocess.run(["pgrep", "-f", needle], capture_output=True, text=True).stdout.split()
    return [int(p) for p in out]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _post(port: int, payload: dict, timeout: float):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        json.dumps(payload).encode(),
        {"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__


def _wait_health(port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1):
                return True
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    return False


class _Broker:
    """A spawned ``python -m broker`` wired to the drill runtime."""

    def __init__(self, tmp_path: Path, *, mode: str, budget: float, startup_timeout: float):
        self.port = _free_port()
        self.runtime_port = _free_port()
        self.escape_pidfile = tmp_path / "escape.pid"
        models = tmp_path / "models"
        models.mkdir(exist_ok=True)
        model_file = models / "m.gguf"
        model_file.write_text("x")
        reg = tmp_path / "registry.yaml"
        reg.write_text(
            "models:\n"
            "  - alias: m\n"
            "    backend: llamacpp\n"
            "    runtime_model_id: m\n"
            "    model_path: " + str(model_file) + "\n"
            "    base_url: http://127.0.0.1:" + str(self.runtime_port) + "\n"
            "    context_limit: 512\n"
            "    output_limit: 16\n"
            "    supports_streaming: true\n"
            '    startup_command: ["' + str(_FIXTURE) + '", "--model", "{model_path}", '
            '"--port", "' + str(self.runtime_port) + '"]\n'
            "    readiness: {kind: http, path: /health, expect_status: 200, "
            "timeout_seconds: " + str(int(startup_timeout)) + "}\n"
        )
        self.env = dict(
            os.environ,
            PYTHONPATH=str(_REPO_ROOT),
            BROKER_HOST="127.0.0.1",
            BROKER_PORT=str(self.port),
            BROKER_REGISTRY_PATH=str(reg),
            BROKER_ALLOWED_EXEC_DIRS=json.dumps([str(_FIXTURE.parent)]),
            BROKER_ALLOWED_MODEL_ROOTS=json.dumps([str(models)]),
            BROKER_CACHE_DIR=str(tmp_path / "cache"),
            BROKER_AUTH_ENABLED="false",
            BROKER_LOG_LEVEL="info",
            BROKER_SHUTDOWN_BUDGET_SECONDS=str(budget),
            BROKER_STARTUP_TIMEOUT_SECONDS=str(startup_timeout),
            OLLAMA_BASE_URL="http://127.0.0.1:11434",
            DRILL_MODE=mode,
            DRILL_ESCAPE_PIDFILE=str(self.escape_pidfile),
        )
        self.logf = open(tmp_path / "broker.log", "w")  # noqa: SIM115
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "broker"],
            cwd=str(_REPO_ROOT),
            env=self.env,
            stdout=self.logf,
            stderr=subprocess.STDOUT,
        )

    def terminate_and_time(self, extra_signals: int = 0, gap: float = 0.3) -> float | None:
        t = time.monotonic()
        self.proc.send_signal(signal.SIGTERM)
        for _ in range(extra_signals):
            time.sleep(gap)
            if self.proc.poll() is None:
                self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(100)
            return round(time.monotonic() - t, 2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            return None  # hung past 100 s (systemd would SIGKILL)

    def cleanup(self):
        for pid in _runtime_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.escape_pidfile.exists():
            try:
                os.kill(int(self.escape_pidfile.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.logf.close()


def test_sigterm_with_loaded_runtime_exits_fast(tmp_path):
    b = _Broker(tmp_path, mode="", budget=45, startup_timeout=30)
    try:
        assert _wait_health(b.port)
        assert (
            _post(b.port, {"model": "m", "messages": [{"role": "user", "content": "hi"}]}, 60)
            == 200
        )
        assert len(_runtime_pids()) == 1
        elapsed = b.terminate_and_time()
        assert elapsed is not None and elapsed < 2.0, f"exit took {elapsed}s"
        time.sleep(0.5)
        assert _runtime_pids() == []
    finally:
        b.cleanup()


def test_runtime_ignoring_sigterm_is_killed_within_budget(tmp_path):
    budget = 5.0
    b = _Broker(tmp_path, mode="ignore_term", budget=budget, startup_timeout=30)
    try:
        assert _wait_health(b.port)
        assert (
            _post(b.port, {"model": "m", "messages": [{"role": "user", "content": "hi"}]}, 60)
            == 200
        )
        elapsed = b.terminate_and_time()
        assert elapsed is not None, "broker hung past 100 s"
        assert elapsed <= budget + 3.0, f"exit took {elapsed}s, over budget {budget}s"
        time.sleep(0.5)
        assert _runtime_pids() == []
    finally:
        b.cleanup()


def test_sigterm_during_slow_load_cancels_and_exits_within_budget(tmp_path):
    budget = 6.0
    b = _Broker(tmp_path, mode="slow_start", budget=budget, startup_timeout=150)
    try:
        assert _wait_health(b.port)
        # Kick a load; the runtime sleeps 100 s before listening, so this hangs.
        import threading

        threading.Thread(
            target=_post,
            args=(b.port, {"model": "m", "messages": [{"role": "user", "content": "hi"}]}, 200),
            daemon=True,
        ).start()
        time.sleep(3)  # load is in progress (runtime spawned, not ready)
        assert len(_runtime_pids()) >= 1
        elapsed = b.terminate_and_time()
        assert elapsed is not None, "broker hung past 100 s (load not cancelled)"
        assert elapsed <= budget + 3.0, f"exit took {elapsed}s, over budget {budget}s"
        time.sleep(0.5)
        assert _runtime_pids() == []
    finally:
        b.cleanup()


def test_escaped_setsid_descendant_is_torn_down(tmp_path):
    b = _Broker(tmp_path, mode="escape", budget=45, startup_timeout=30)
    try:
        assert _wait_health(b.port)
        assert (
            _post(b.port, {"model": "m", "messages": [{"role": "user", "content": "hi"}]}, 60)
            == 200
        )
        # Give the descendant recorder a snapshot while the parent is alive.
        time.sleep(1.5)
        assert b.escape_pidfile.exists(), "escaped grandchild never started"
        grandchild = int(b.escape_pidfile.read_text())
        assert _pid_alive(grandchild)
        elapsed = b.terminate_and_time()
        assert elapsed is not None
        time.sleep(0.5)
        left = _runtime_pids()
        if _IS_LINUX:
            # Subreaper + /proc: the whole tree, grandchild included, is gone.
            assert left == [], f"survivors: {left}"
            assert not _pid_alive(grandchild)
        else:
            # macOS: killed because it was recorded while its parent lived. If a
            # race left it unrecorded, it must be reported (never silently left),
            # but with a 1.5 s recorder window it should be gone here.
            assert not _pid_alive(grandchild), "recorded descendant should be killed"
    finally:
        b.cleanup()


def test_open_stream_at_sigterm_exits_within_budget(tmp_path):
    budget = 10.0
    b = _Broker(tmp_path, mode="", budget=budget, startup_timeout=30)
    try:
        assert _wait_health(b.port)
        assert (
            _post(b.port, {"model": "m", "messages": [{"role": "user", "content": "hi"}]}, 60)
            == 200
        )
        import threading

        threading.Thread(
            target=_post,
            args=(
                b.port,
                {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True},
                300,
            ),
            daemon=True,
        ).start()
        time.sleep(3)  # a stream is open and flowing
        elapsed = b.terminate_and_time()
        assert elapsed is not None, "broker hung past 100 s with an open stream"
        assert elapsed <= budget + 3.0, f"exit took {elapsed}s, over budget {budget}s"
        time.sleep(0.5)
        assert _runtime_pids() == []
    finally:
        b.cleanup()


def test_second_sigterm_forces_immediate_kill(tmp_path):
    # A large budget would normally make an ignore_term runtime wait; the second
    # signal must escalate to SIGKILL and exit quickly regardless.
    b = _Broker(tmp_path, mode="ignore_term", budget=30, startup_timeout=30)
    try:
        assert _wait_health(b.port)
        assert (
            _post(b.port, {"model": "m", "messages": [{"role": "user", "content": "hi"}]}, 60)
            == 200
        )
        elapsed = b.terminate_and_time(extra_signals=1, gap=0.5)
        assert elapsed is not None, "broker hung past 100 s"
        assert elapsed < 4.0, f"second SIGTERM did not force a fast exit: {elapsed}s"
        time.sleep(0.5)
        assert _runtime_pids() == []
    finally:
        b.cleanup()
