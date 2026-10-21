"""ProcessSupervisor.stop() now reports whether the process is gone (ADR 0020)."""

from __future__ import annotations

from broker.proc import ProcessSupervisor


async def test_stop_returns_true_when_process_exits():
    sup = ProcessSupervisor(["/bin/sleep", "30"], alias="t", backend="test")
    pid = await sup.start()
    assert pid and sup.is_running()
    assert sup.pgid is not None
    released = await sup.stop(grace=2.0)
    assert released is True
    assert not sup.is_running()


async def test_stop_on_already_dead_is_true_and_idempotent():
    sup = ProcessSupervisor(["/bin/sh", "-c", "exit 0"], alias="t", backend="test")
    await sup.start()
    assert await sup.stop() is True
    # A second stop is a no-op that still reports released.
    assert await sup.stop() is True
