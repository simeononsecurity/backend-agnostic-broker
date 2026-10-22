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
from collections.abc import Callable, Sequence

from . import runtime_identity as rid
from . import teardown
from .errors import ModelLoadError
from .teardown import TeardownResult

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
        # ADR 0024: this runtime's own identity anchor (PID + start time) and a
        # running record of every descendant seen while it was alive, so an escaped
        # setsid child is still in the kill set after its parent dies.
        self._main_start_time: str | None = None
        self._descendants: dict[int, str] = {}
        self._recorder_task: asyncio.Task | None = None
        self._recorder_stop: asyncio.Event | None = None

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
        self._main_start_time = rid.process_start_time(self._proc.pid)
        self._descendants = {}
        self._recorder_stop = asyncio.Event()
        self._recorder_task = asyncio.ensure_future(self._record_descendants())
        return self._proc.pid

    async def _record_descendants(self) -> None:
        """Periodically record this runtime's descendants while it is alive.

        A descendant that later escapes its group (``setsid``) or is re-parented
        is remembered here (PID + start time), so teardown can still find and
        identity-gate it after the parent process is gone. Argv-only ``ps`` off
        the event loop on non-Linux; ``/proc`` on Linux (ADR 0024).
        """
        assert self._recorder_stop is not None
        main_pid = self._proc.pid if self._proc else None
        while not self._recorder_stop.is_set():
            if main_pid is not None:
                try:
                    live = await asyncio.to_thread(teardown.enumerate_descendants, main_pid)
                except Exception:  # noqa: BLE001 - best effort
                    live = set()
                for pid in live:
                    if pid not in self._descendants:
                        st = await asyncio.to_thread(rid.process_start_time, pid)
                        if st:
                            self._descendants[pid] = st
            try:
                await asyncio.wait_for(self._recorder_stop.wait(), timeout=1.0)
            except TimeoutError:
                pass

    async def _stop_recorder(self) -> None:
        if self._recorder_stop is not None:
            self._recorder_stop.set()
        if self._recorder_task and not self._recorder_task.done():
            try:
                await asyncio.wait_for(self._recorder_task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                self._recorder_task.cancel()

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

    async def stop(
        self,
        grace: float = 10.0,
        *,
        hard_deadline: float | None = None,
        sigkill_only: bool = False,
        force_check: Callable[[], bool] | None = None,
    ) -> TeardownResult:
        """Tear down the runtime's whole tree within a deadline (ADR 0024).

        The kill set is the process group **plus every recorded descendant**
        (an escaped ``setsid`` child included), each identity-gated. ``SIGTERM``
        then ``SIGKILL`` once ``grace`` elapses, the overall ``hard_deadline``
        (monotonic) passes, or ``force_check`` returns True. The blocking signal
        loop runs off the event loop via ``asyncio.to_thread``. Returns a
        :class:`TeardownResult` describing what was signalled, what exited, and
        any survivors (a stuck device), instead of silently swallowing leftovers.
        """
        main_pid = self._proc.pid if self._proc else None
        pgid = self._started_pgid
        start_time = self._main_start_time
        # Stop recording first so the descendant snapshot is final, then grab it.
        await self._stop_recorder()
        descendants = dict(self._descendants)

        if main_pid is None:
            await self._cleanup_reader()
            return TeardownResult(alias=self.alias or "", outcome="noop")

        result = await asyncio.to_thread(
            teardown.terminate_tree,
            main_pid=main_pid,
            main_start_time=start_time,
            pgid=pgid,
            descendants=descendants,
            grace=grace,
            hard_deadline=hard_deadline,
            sigkill_only=sigkill_only,
            force_check=force_check,
            alias=self.alias or "",
            backend=self.backend or "",
            # The main PID is our own asyncio.subprocess child; asyncio's child
            # watcher reaps it (via self._proc.wait() below), so terminate_tree
            # must not waitpid it — only its re-parented descendants (ADR 0024).
            asyncio_child_pid=main_pid,
        )
        # Let asyncio reap our own child so its transport is finalized (no zombie).
        if self._proc is not None and self._proc.returncode is None:
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=1.0)
            except (TimeoutError, Exception):  # noqa: BLE001
                pass
        await self._cleanup_reader()
        return result

    async def _cleanup_reader(self) -> None:
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
