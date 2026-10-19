"""Unit tests for the SQLite usage/decision log."""

from __future__ import annotations

from broker.usage import UsageLog


def test_record_and_summary():
    u = UsageLog(":memory:", enabled=True)
    u.record(
        endpoint="chat",
        requested="auto",
        model="echo",
        prompt_tokens=3,
        completion_tokens=2,
        total_tokens=5,
        status="ok",
    )
    u.record(endpoint="completions", requested="echo", model="echo", total_tokens=4)
    s = u.summary()
    assert s["enabled"] is True
    assert s["requests"] == 2
    assert s["total_tokens"] == 9
    by_model = {m["model"]: m for m in s["by_model"]}
    assert by_model["echo"]["requests"] == 2
    by_endpoint = {e["endpoint"] for e in s["by_endpoint"]}
    assert {"chat", "completions"} <= by_endpoint
    assert len(s["recent"]) == 2
    u.close()


def test_disabled_is_noop():
    u = UsageLog(None, enabled=False)
    u.record(endpoint="chat", model="echo")  # must not raise
    assert u.summary() == {"enabled": False}
    u.close()


def test_key_fingerprint_is_non_reversible():
    assert UsageLog.key_fingerprint(None) is None
    fp = UsageLog.key_fingerprint("super-secret-key")
    assert fp is not None
    assert len(fp) == 12
    assert "secret" not in fp
    # stable
    assert fp == UsageLog.key_fingerprint("super-secret-key")


def test_write_failure_is_swallowed(tmp_path):
    u = UsageLog(str(tmp_path / "usage.sqlite3"), enabled=True)
    u.close()  # close the connection out from under record()
    # record after close must not raise (best-effort logging)
    u.record(endpoint="chat", model="echo", total_tokens=1)
