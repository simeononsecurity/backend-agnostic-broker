"""Runtime identity + ledger unit tests (ADR 0021).

Covers the cross-platform start-time parsers (fixtures), the ``kill(0)`` liveness
+ identity gate against real child processes, identity-gated process-group
termination, and the atomic/locked/corruption-safe runtime ledger.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from broker import runtime_identity as rid
from broker.ledger import LEDGER_VERSION, RuntimeLedger, RuntimeRecord, argv_hash


# --- start-time parsers (fixtures, no live process) ----------------------
def test_parse_linux_starttime_handles_comm_with_spaces_and_parens():
    # comm (field 2) contains a space and a ')'; field 22 (starttime) is the
    # 20th token after the LAST ')'. Build 20 tokens: index 19 == starttime.
    after = ["S"] + [str(i) for i in range(1, 20)]  # index 19 -> "19" ticks
    stat_line = "1234 (weird )name) " + " ".join(after)
    val = rid.parse_linux_starttime(stat_line, btime=1_000_000.0, clk_tck=100)
    assert val == pytest.approx(1_000_000 + 19 / 100, abs=0.001)


def test_parse_linux_starttime_short_line_returns_none():
    assert rid.parse_linux_starttime("1 (x) S 0 0", btime=0.0, clk_tck=100) is None
    assert rid.parse_linux_starttime("no paren here", btime=0.0, clk_tck=100) is None


def test_parse_linux_boot_time():
    text = "cpu  1 2 3\nbtime 1600000000\nprocesses 42\n"
    assert rid.parse_linux_boot_time(text) == 1_600_000_000.0
    assert rid.parse_linux_boot_time("no btime line\n") is None


def test_parse_macos_lstart_collapses_whitespace():
    assert rid.parse_macos_lstart("  Tue Oct 20   18:57:03  2026 \n") == "Tue Oct 20 18:57:03 2026"
    assert rid.parse_macos_lstart("   ") is None


def test_port_helpers():
    assert rid.port_from_url("http://127.0.0.1:8123/v1") == 8123
    assert rid.port_from_url("https://example") == 443
    assert rid.port_from_url("http://host") == 80
    assert rid.port_from_url(None) is None
    assert rid.host_from_url("http://127.0.0.1:9/x") == "127.0.0.1"


# --- liveness + identity against a real child ----------------------------
@pytest.fixture
def sleeper():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.2)
    yield proc
    try:
        os.killpg(os.getpgid(proc.pid), 9)
    except (ProcessLookupError, OSError):
        pass
    proc.wait(timeout=5)


def test_pid_alive_and_start_time(sleeper):
    assert rid.pid_alive(sleeper.pid)
    st = rid.process_start_time(sleeper.pid)
    assert st is not None
    # Stable across reads for the same process.
    assert rid.process_start_time(sleeper.pid) == st
    assert rid.identity_matches(sleeper.pid, st)


def test_identity_mismatch_is_fail_closed(sleeper):
    # A record with the WRONG start time must never be treated as ours.
    assert not rid.identity_matches(sleeper.pid, "0.00")
    assert not rid.identity_matches(sleeper.pid, None)


def test_pid_alive_false_for_dead_pid():
    assert not rid.pid_alive(2_000_000_000)
    assert rid.process_start_time(2_000_000_000) is None


def test_terminate_process_group_identity_gated(sleeper):
    st = rid.process_start_time(sleeper.pid)
    pgid = rid.pgid_of(sleeper.pid)
    # Wrong identity: refuse to signal; the process stays alive.
    assert rid.terminate_process_group(sleeper.pid, pgid, "0.00", grace=1.0) == "identity_mismatch"
    assert rid.pid_alive(sleeper.pid)
    # Correct identity: terminated.
    status = rid.terminate_process_group(sleeper.pid, pgid, st, grace=3.0)
    assert status in ("terminated", "killed")
    time.sleep(0.1)
    # Dead — a zombie (unreaped by the test parent) still answers kill(0) but is
    # effectively terminated; the fixture's proc.wait() reaps it.
    assert rid.process_is_zombie(sleeper.pid) or not rid.pid_alive(sleeper.pid)


def test_port_in_use_detects_a_listener():
    import socket

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert rid.port_in_use("127.0.0.1", port)
    finally:
        srv.close()
    # After close, nothing is listening.
    assert not rid.port_in_use("127.0.0.1", port)


# --- ledger: atomic, locked, corruption-safe -----------------------------
def _rec(alias="m1", pid=4321):
    return RuntimeRecord(
        alias=alias,
        pid=pid,
        pgid=pid,
        start_time="123.45",
        port=8123,
        argv_sha256=argv_hash(["/usr/bin/x", "--flag"]),
        broker_instance_id="abc123",
        base_url="http://127.0.0.1:8123",
        spawned_at=time.time(),
    )


def test_ledger_roundtrip_and_remove(tmp_path):
    led = RuntimeLedger(str(tmp_path / "runtime-ledger.json"))
    assert led.load() == []
    led.upsert(_rec("a", 1))
    led.upsert(_rec("b", 2))
    aliases = {r.alias for r in led.load()}
    assert aliases == {"a", "b"}
    # Upsert replaces by alias (one runtime per alias).
    led.upsert(_rec("a", 99))
    rec_a = next(r for r in led.load() if r.alias == "a")
    assert rec_a.pid == 99
    led.remove("a")
    assert {r.alias for r in led.load()} == {"b"}
    led.clear()
    assert led.load() == []


def test_ledger_file_is_0600(tmp_path):
    path = tmp_path / "runtime-ledger.json"
    led = RuntimeLedger(str(path))
    led.upsert(_rec())
    mode = os.stat(path).st_mode & 0o777
    assert mode == 0o600


def test_ledger_corrupt_is_ignored(tmp_path):
    path = tmp_path / "runtime-ledger.json"
    path.write_text("{ this is : not json ]")
    led = RuntimeLedger(str(path))
    # Fail-safe: a torn ledger reads as empty (never raises, never acted on).
    assert led.load() == []


def test_ledger_unknown_version_ignored(tmp_path):
    import json

    path = tmp_path / "runtime-ledger.json"
    path.write_text(json.dumps({"version": LEDGER_VERSION + 99, "runtimes": [_rec().to_dict()]}))
    led = RuntimeLedger(str(path))
    assert led.load() == []


def test_ledger_none_path_is_noop():
    led = RuntimeLedger(None)
    led.upsert(_rec())  # no raise
    assert led.load() == []
