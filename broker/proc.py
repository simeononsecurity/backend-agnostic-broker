"""Async child-process supervisor.

Launches runtime binaries as an argv **list** (never a shell string), in a new
session/process-group so the whole tree can be signalled, and terminates
gracefully (SIGTERM then SIGKILL after a grace period). Failed/exited processes
are detected and cleaned up.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Sequence

from .errors import ModelLoadError

log = logging.getLogger("broker.proc")


class ProcessSupervisor:
    def __init__(
        self,
        argv: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        alias: str = "",
        backend: str = "",
    ) -> None:
        if not argv:
            raise ValueError("argv must be non-empty")
        self.argv = list(argv)
        self.env = env
        self.cwd = cwd
        self.alias = alias
        self.backend = backend
        self._proc: asyncio.subprocess.Process | None = None
        self._log_lines: list[str] = []
        self._reader_task: asyncio.Task | None = None
        # ADR 0020: the process-group id captured at spawn (start_new_session makes
        # the child a group leader, so pgid == pid). Kept after stop() so the
        # measured-release wait can still scan /proc for lingering group members.
        self._started_pgid: int | None = None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def started_pgid(self) -> int | None:
        """The process-group id recorded at spawn (survives stop/exit)."""
        return self._started_pgid

    @property
    def pgid(self) -> int | None:
        """Process-group id of the spawned tree, or None if not running/available."""
        if not self._proc:
            return None
        try:
            return os.getpgid(self._proc.pid)
        except (ProcessLookupError, PermissionError, OSError):
            return None

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc else None

    def recent_log(self, n: int = 40) -> str:
        return "\n".join(self._log_lines[-n:])

    async def start(self) -> int:
        full_env = dict(os.environ)
        if self.env:
            full_env.update(self.env)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=full_env,
                cwd=self.cwd,
                # New session => new process group; lets us killpg the tree and
                # isolates the child from the broker's controlling terminal.
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise ModelLoadError(
                f"runtime executable not found: {self.argv[0]!r}",
                backend=self.backend,
                model=self.alias,
                remediation="install the runtime or fix its configured path",
            ) from exc
        self._reader_task = asyncio.ensure_future(self._drain_output())
        try:
            self._started_pgid = os.getpgid(self._proc.pid)
        except OSError:  # pragma: no cover - race if the child exited instantly
            self._started_pgid = self._proc.pid
        return self._proc.pid

    async def _drain_output(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            async for raw in self._proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                self._log_lines.append(line)
                if len(self._log_lines) > 1000:
                    self._log_lines = self._log_lines[-1000:]
        except Exception:  # noqa: BLE001 - reader is best-effort
            pass

    async def wait_for_log(self, marker: str, timeout: float) -> None:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if any(marker in ln for ln in self._log_lines):
                return
            if not self.is_running():
                raise ModelLoadError(
                    f"process exited (code {self.returncode}) before log marker",
                    backend=self.backend,
                    model=self.alias,
                    remediation="inspect runtime logs: " + self.recent_log(10),
                )
            await asyncio.sleep(0.2)
        raise ModelLoadError(
            f"timed out waiting for log marker {marker!r}",
            backend=self.backend,
            model=self.alias,
            remediation="increase startup_timeout or check runtime logs",
        )

    async def stop(self, grace: float = 10.0) -> bool:
        """Terminate the process group: SIGTERM, then SIGKILL after ``grace``.

        Returns ``True`` when the process is confirmed gone, ``False`` when it is
        still alive after SIGKILL + a short reap wait. ADR 0020: a surviving
        process is **logged at error level and reported** to the caller instead of
        being silently swallowed (the old behavior), so a stuck teardown is visible.
        """
        if not self._proc or self._proc.returncode is not None:
            await self._cleanup_reader()
            return True
        pid = self._proc.pid
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=grace)
        except TimeoutError:
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except TimeoutError:
                pass
        await self._cleanup_reader()
        alive = self._proc.returncode is None
        if alive:
            log.error(
                "process for %s (backend=%s pid=%s) is still alive after SIGKILL; "
                "its device memory may not be released",
                self.alias or "?",
                self.backend or "?",
                pid,
            )
        return not alive

    async def _cleanup_reader(self) -> None:
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
