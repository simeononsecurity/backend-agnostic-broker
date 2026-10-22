"""Shared real-process harness for the ADR 0027 lifecycle failure drills.

Spawns a real ``python -m broker`` wired to one or more stand-in runtimes
(``tests/fixtures/drill_runtime.py``) and, optionally, a recorded-fixture
``nvidia-smi`` (``tests/fixtures/fake_nvidia_smi.sh``) and device leases. The
drills in :mod:`tests.drills.test_lifecycle_drills` use this to assert, for each
scenario, a wall-clock bound, **zero leftover runtime processes** (checked by PID
+ start time), an **empty ledger and lease directory** afterwards, and the typed
error where one is expected.

POSIX only (fork / signals). The Linux-specific descendant paths also run in the
project's Python 3.11 container gate.
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

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_FIXTURES = _REPO_ROOT / "tests" / "fixtures"
_RUNTIME = _FIXTURES / "drill_runtime.py"
_FAKE_NVIDIA = _FIXTURES / "fake_nvidia_smi.sh"
_IS_LINUX = sys.platform.startswith("linux")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def runtime_pids() -> list[int]:
    """Live drill-runtime PIDs (parent + any escaped descendant).

    Uses ``/proc`` on Linux (``python:3.11-slim`` has no ``pgrep``) and falls
    back to ``pgrep -f`` elsewhere. Matched by the runtime fixture path so only
    our stand-ins are counted.
    """
    needle = str(_RUNTIME)
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


class BrokerProc:
    """A spawned ``python -m broker`` wired to drill runtime(s), GPU + leases."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        mode: str = "",
        budget: float = 45.0,
        startup_timeout: float = 30.0,
        aliases: tuple[str, ...] = ("m",),
        idle_timeout: float | None = None,
        gpu: dict | None = None,
        gpu_telemetry: str | None = None,
        gpu_devices: str | None = None,
        gpu_verify_seconds: float | None = None,
        fake_nvidia_gpus: str | None = None,
        fake_nvidia_apps: str | None = None,
        leases: bool = False,
        reconcile: str | None = None,
        lease_acquire_command: str | None = None,
        lease_release_command: str | None = None,
        cache_dir: Path | None = None,
        extra_env: dict | None = None,
    ):
        self.tmp_path = tmp_path
        self.port = free_port()
        self.escape_pidfile = tmp_path / "escape.pid"
        self.aliases = aliases
        self.runtime_ports = {a: free_port() for a in aliases}
        self.runtime_port = self.runtime_ports[aliases[0]]
        self.cache_dir = Path(cache_dir) if cache_dir is not None else tmp_path / "cache"
        self.lease_dir = tmp_path / "leases"
        models = tmp_path / "models"
        models.mkdir(exist_ok=True)
        model_file = models / "m.gguf"
        model_file.write_text("x")
        reg = tmp_path / "registry.yaml"
        lines = ["models:\n"]
        for alias in aliases:
            rp = self.runtime_ports[alias]
            block = (
                "  - alias: " + alias + "\n"
                "    backend: llamacpp\n"
                "    runtime_model_id: " + alias + "\n"
                "    model_path: " + str(model_file) + "\n"
                "    base_url: http://127.0.0.1:" + str(rp) + "\n"
                "    context_limit: 512\n"
                "    output_limit: 16\n"
                "    supports_streaming: true\n"
                '    startup_command: ["' + str(_RUNTIME) + '", "--model", "{model_path}", '
                '"--port", "' + str(rp) + '"]\n'
                "    readiness: {kind: http, path: /health, expect_status: 200, "
                "timeout_seconds: " + str(int(startup_timeout)) + "}\n"
            )
            if idle_timeout is not None:
                block += "    idle_timeout_seconds: " + str(idle_timeout) + "\n"
            if gpu:
                block += "    gpu:\n"
                for k, v in gpu.items():
                    block += f"      {k}: {json.dumps(v)}\n"
            lines.append(block)
        reg.write_text("".join(lines))

        allowed_exec = [str(_FIXTURES)]
        env = dict(
            os.environ,
            PYTHONPATH=str(_REPO_ROOT),
            BROKER_HOST="127.0.0.1",
            BROKER_PORT=str(self.port),
            BROKER_REGISTRY_PATH=str(reg),
            BROKER_ALLOWED_EXEC_DIRS=json.dumps(allowed_exec),
            BROKER_ALLOWED_MODEL_ROOTS=json.dumps([str(models)]),
            BROKER_CACHE_DIR=str(self.cache_dir),
            BROKER_AUTH_ENABLED="false",
            BROKER_LOG_LEVEL="info",
            BROKER_SHUTDOWN_BUDGET_SECONDS=str(budget),
            BROKER_STARTUP_TIMEOUT_SECONDS=str(startup_timeout),
            OLLAMA_BASE_URL="http://127.0.0.1:11434",
            DRILL_MODE=mode,
            DRILL_ESCAPE_PIDFILE=str(self.escape_pidfile),
        )
        if gpu_telemetry:
            env["BROKER_GPU_TELEMETRY"] = gpu_telemetry
            env["BROKER_GPU_TELEMETRY_NVIDIA_SMI_PATH"] = str(_FAKE_NVIDIA)
            env["BROKER_GPU_TELEMETRY_POLL_SECONDS"] = "1"
        if gpu_devices is not None:
            env["BROKER_GPU_DEVICES"] = gpu_devices
        if gpu_verify_seconds is not None:
            env["BROKER_GPU_VERIFY_SECONDS"] = str(gpu_verify_seconds)
        if fake_nvidia_gpus is not None:
            env["FAKE_NVIDIA_GPUS"] = fake_nvidia_gpus
        if fake_nvidia_apps is not None:
            env["FAKE_NVIDIA_APPS"] = fake_nvidia_apps
        if leases:
            env["BROKER_DEVICE_LEASES"] = "on"
            env["BROKER_DEVICE_LEASE_DIR"] = str(self.lease_dir)
        if reconcile is not None:
            env["BROKER_RUNTIME_RECONCILE"] = reconcile
        if lease_acquire_command is not None:
            env["BROKER_LEASE_ACQUIRE_COMMAND"] = lease_acquire_command
        if lease_release_command is not None:
            env["BROKER_LEASE_RELEASE_COMMAND"] = lease_release_command
        if extra_env:
            env.update({k: str(v) for k, v in extra_env.items()})
        self.env = env
        self.logf = open(tmp_path / "broker.log", "w")  # noqa: SIM115
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "broker"],
            cwd=str(_REPO_ROOT),
            env=env,
            stdout=self.logf,
            stderr=subprocess.STDOUT,
        )

    # --- client helpers -------------------------------------------------
    def wait_health(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=1):
                    return True
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        return False

    def post(self, payload: dict, timeout: float, alias: str = "m"):
        """POST a chat completion; return the HTTP status, or the exception name."""
        body = dict(payload)
        body.setdefault("model", alias)
        body.setdefault("messages", [{"role": "user", "content": "hi"}])
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            json.dumps(body).encode(),
            {"content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except Exception as exc:  # noqa: BLE001
            return type(exc).__name__

    def error_type(self, payload: dict, timeout: float, alias: str = "m") -> str | None:
        """POST and return the broker's typed ``error.type`` from a 4xx/5xx body."""
        body = dict(payload)
        body.setdefault("model", alias)
        body.setdefault("messages", [{"role": "user", "content": "hi"}])
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            json.dumps(body).encode(),
            {"content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout):
                return None
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read()).get("error", {}).get("type")
            except Exception:  # noqa: BLE001
                return f"http_{exc.code}"
        except Exception as exc:  # noqa: BLE001
            return type(exc).__name__

    def status(self, timeout: float = 5.0) -> dict:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/status", timeout=timeout
            ) as resp:
                return json.loads(resp.read())
        except Exception:  # noqa: BLE001
            return {}

    # --- lifecycle ------------------------------------------------------
    def terminate_and_time(self, extra_signals: int = 0, gap: float = 0.3) -> float | None:
        """SIGTERM the broker (optionally a second time) and time the exit.

        Returns the elapsed seconds, or ``None`` if the broker hung past 100 s
        (systemd would then SIGKILL — a failure for the drill).
        """
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
            return None

    def sigkill_broker(self) -> None:
        """SIGKILL the broker (ungraceful crash) — leaves the runtime orphaned."""
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait()

    def read_escape_pid(self) -> int | None:
        try:
            return int(self.escape_pidfile.read_text())
        except (OSError, ValueError):
            return None

    # --- post-conditions ------------------------------------------------
    def ledger_records(self) -> list[dict]:
        """Live runtime records in the ADR 0021 ledger (empty after clean stop)."""
        path = self.cache_dir / "runtime-ledger.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return []
        if isinstance(data, dict):
            recs = data.get("runtimes") or data.get("records") or []
        else:
            recs = data
        return list(recs) if isinstance(recs, list) else []

    def lease_files(self) -> list[Path]:
        if not self.lease_dir.exists():
            return []
        return [p for p in self.lease_dir.iterdir() if p.name.endswith(".lease.json")]

    def run_cleanup(self, timeout: float = 20.0, report: bool = False) -> dict:
        """Run ``python -m broker cleanup`` with this broker's env; return the JSON."""
        argv = [sys.executable, "-m", "broker", "cleanup", "--timeout", str(timeout)]
        if report:
            argv.append("--report")
        out = subprocess.run(
            argv,
            cwd=str(_REPO_ROOT),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout + 10,
        )
        try:
            return json.loads(out.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"_stdout": out.stdout, "_stderr": out.stderr}

    def cleanup(self) -> None:
        """Kill any surviving runtime + the broker. Always safe to call twice."""
        for pid in runtime_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        esc = self.read_escape_pid()
        if esc is not None:
            try:
                os.kill(esc, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        try:
            self.logf.close()
        except Exception:  # noqa: BLE001
            pass
