"""ProcessSupervisor.stop() reports a teardown result (ADR 0020 → ADR 0024).

ADR 0024 replaced the bool return with a :class:`TeardownResult` describing what
was signalled, what exited, and any survivors; ``result.ok()`` is the successor
of the old "True when the process is gone" contract.
"""

from __future__ import annotations

from broker.proc import ProcessSupervisor
from broker.teardown import TeardownResult


async def test_stop_returns_result_when_process_exits():
    sup = ProcessSupervisor(["/bin/sleep", "30"], alias="t", backend="test")
    pid = await sup.start()
    assert pid and sup.is_running()
    assert sup.pgid is not None
    result = await sup.stop(grace=2.0)
    assert isinstance(result, TeardownResult)
    assert result.ok()
    assert not result.survivors
    assert pid in result.exited
    assert not sup.is_running()


async def test_stop_on_already_dead_is_ok_and_idempotent():
    sup = ProcessSupervisor(["/bin/sh", "-c", "exit 0"], alias="t", backend="test")
    await sup.start()
    first = await sup.stop()
    assert first.ok()
    # A second stop is a no-op that still reports ok (nothing survived).
    second = await sup.stop()
    assert second.ok()
