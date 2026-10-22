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
import urllib.error
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


def _post_status(port: int, payload: dict, timeout: float):
    """POST a chat completion, returning the HTTP status code even on an error.

    Unlike :func:`_post`, an HTTP error response (e.g. a 503 ``shutting_down``)
    yields its integer status rather than the exception name, so a test can tell
    a fast 503 apart from a dropped connection / hang.
    """
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        json.dumps(payload).encode(),
        {"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
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
    """A spawned ``python -m broker`` wired to one or more drill runtimes."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        mode: str,
        budget: float,
        startup_timeout: float,
        aliases: tuple[str, ...] = ("m",),
        extra_env: dict | None = None,
    ):
        self.port = _free_port()
        self.escape_pidfile = tmp_path / "escape.pid"
        self.aliases = aliases
        self.runtime_ports = {a: _free_port() for a in aliases}
        # Back-compat for single-model tests.
        self.runtime_port = self.runtime_ports[aliases[0]]
        models = tmp_path / "models"
        models.mkdir(exist_ok=True)
        model_file = models / "m.gguf"
        model_file.write_text("x")
        reg = tmp_path / "registry.yaml"
        lines = ["models:\n"]
        for alias in aliases:
            rp = self.runtime_ports[alias]
            lines.append(
                "  - alias: " + alias + "\n"
                "    backend: llamacpp\n"
                "    runtime_model_id: " + alias + "\n"
                "    model_path: " + str(model_file) + "\n"
                "    base_url: http://127.0.0.1:" + str(rp) + "\n"
                "    context_limit: 512\n"
                "    output_limit: 16\n"
                "    supports_streaming: true\n"
                '    startup_command: ["' + str(_FIXTURE) + '", "--model", "{model_path}", '
                '"--port", "' + str(rp) + '"]\n'
                "    readiness: {kind: http, path: /health, expect_status: 200, "
                "timeout_seconds: " + str(int(startup_timeout)) + "}\n"
            )
        reg.write_text("".join(lines))
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
        if extra_env:
            self.env.update({k: str(v) for k, v in extra_env.items()})
        self.logf = open(tmp_path / "broker.log", "w")  # noqa: SIM115
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "broker"],
            cwd=str(_REPO_ROOT),
            env=self.env,
            stdout=self.logf,
            stderr=subprocess.STDOUT,
        )

    def read_escape_pid(self) -> int | None:
        try:
            return int(self.escape_pidfile.read_text())
        except (OSError, ValueError):
            return None

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
        # The first signal preempts the in-progress load immediately (ADR 0024),
        # so the broker exits well under budget — not just within budget + slack.
        assert elapsed < 5.0, f"exit took {elapsed}s; load was not preempted at the first signal"
        time.sleep(0.5)
        assert _runtime_pids() == []
    finally:
        b.cleanup()


def test_sigterm_while_client_waits_on_cold_load_returns_503_fast(tmp_path):
    # GAP 1 (ADR 0024): a client request blocked on a cold load must fail fast
    # with a typed 503 the moment the broker starts stopping, and the broker must
    # exit well under the budget — NOT hang until the full budget expires (the
    # pre-fix behaviour, which spent the whole budget in uvicorn's graceful phase
    # before cancelling the load).
    import threading

    budget = 45.0
    b = _Broker(tmp_path, mode="slow_start", budget=budget, startup_timeout=150)
    result: dict = {}
    try:
        assert _wait_health(b.port)

        def _call():
            result["status"] = _post_status(
                b.port, {"model": "m", "messages": [{"role": "user", "content": "hi"}]}, 120
            )

        t = threading.Thread(target=_call, daemon=True)
        t.start()
        time.sleep(3)  # the client is now blocked on the cold load
        assert len(_runtime_pids()) >= 1
        elapsed = b.terminate_and_time()
        assert elapsed is not None, "broker hung past 100 s"
        assert elapsed < 5.0, f"exit took {elapsed}s with a {budget}s budget (load not preempted)"
        t.join(10)
        assert result.get("status") == 503, (
            f"waiting client got {result.get('status')!r}, expected a fast 503 shutting_down"
        )
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


def _zombie_children(ppid: int) -> list[int]:
    """PIDs of zombie processes whose current parent is ``ppid`` (Linux /proc)."""
    out: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8", errors="replace") as fh:
                data = fh.read()
        except OSError:
            continue
        rest = data[data.rfind(")") + 1 :].split()
        if len(rest) >= 2 and rest[0] == "Z":
            try:
                if int(rest[1]) == ppid:
                    out.append(int(entry))
            except ValueError:
                continue
    return out


@pytest.mark.skipif(not _IS_LINUX, reason="subreaper re-parenting + /proc zombies are Linux-only")
def test_evictions_reap_reparented_descendants_no_zombies(tmp_path):
    # GAP 2 (ADR 0024): with the broker a child subreaper, an evicted runtime's
    # escaped setsid grandchild re-parents to the broker and, once killed, would
    # linger as a zombie child unless the broker reaps it. Alternating evictions
    # must leave zero zombie children of the broker while still serving each
    # request (normal runtime lifecycle is observed correctly).
    b = _Broker(
        tmp_path,
        mode="escape",
        budget=10,
        startup_timeout=30,
        aliases=("a", "z"),
        extra_env={"BROKER_MAX_LOADED_MODELS": 1, "BROKER_SHUTDOWN_TIMEOUT_SECONDS": 3},
    )
    try:
        assert _wait_health(b.port)
        grandchildren: list[int] = []
        codes: list = []
        for i in range(6):
            alias = "a" if i % 2 == 0 else "z"
            codes.append(
                _post_status(
                    b.port, {"model": alias, "messages": [{"role": "user", "content": "hi"}]}, 60
                )
            )
            time.sleep(0.8)  # let the runtime ready + the descendant recorder snapshot
            gp = b.read_escape_pid()
            if gp is not None:
                grandchildren.append(gp)
        # Every request was served (the runtime lifecycle still works under churn).
        assert codes == [200] * 6, f"eviction chat codes: {codes}"
        # Give the sweeper a tick as a backstop in case a reap raced a teardown.
        time.sleep(2.0)
        zombies = _zombie_children(b.proc.pid)
        assert zombies == [], f"re-parented descendants left as zombies: {zombies}"
        # Each evicted model's grandchild (all but possibly the last, still loaded)
        # must be dead — not merely unreaped.
        evicted = grandchildren[:-1] if grandchildren else []
        still_alive = [p for p in evicted if _pid_alive(p)]
        assert still_alive == [], f"evicted grandchildren still alive: {still_alive}"
    finally:
        b.cleanup()
