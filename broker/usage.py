"""Request usage + routing-decision log (SQLite, stdlib-only).

Records one row per completed request: timestamp, endpoint, requested vs served
model, token counts, latency, status, and (for ``auto``) how the model was
chosen. This is the data substrate for cost reporting, budgets, and router
evaluation (see ADR 0007).

Privacy: only metadata and token *counts* are stored — never request or response
content. The API-key fingerprint is a short, non-reversible SHA-256 prefix, never
the key itself. The log is best-effort: a write failure is logged and swallowed so
it can never break a request. Set ``usage_log_path`` to ``":memory:"`` for an
ephemeral log, or disable with ``usage_log_enabled=false``.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from typing import Any

log = logging.getLogger("broker.usage")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  request_id TEXT,
  endpoint TEXT NOT NULL,
  requested TEXT,
  model TEXT,
  api_key_id TEXT,
  prompt_tokens INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0,
  total_tokens INTEGER DEFAULT 0,
  latency_ms REAL,
  status TEXT,
  stream INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);
CREATE INDEX IF NOT EXISTS idx_usage_model ON usage(model);
"""


class UsageLog:
    """Append-only usage log backed by SQLite; thread-safe, best-effort writes."""

    def __init__(self, path: str | None, enabled: bool = True) -> None:
        self.enabled = enabled
        self.path = path or ":memory:"
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        if not enabled:
            return
        try:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001 - never block startup on the log
            log.warning("usage log init failed (%s): disabling", exc)
            self.enabled = False
            self._conn = None

    @staticmethod
    def key_fingerprint(api_key: str | None) -> str | None:
        """Short, non-reversible fingerprint of an API key (never the key)."""
        if not api_key:
            return None
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]

    def record(
        self,
        *,
        endpoint: str,
        requested: str | None = None,
        model: str | None = None,
        request_id: str | None = None,
        api_key_id: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        latency_ms: float | None = None,
        status: str = "ok",
        stream: bool = False,
    ) -> None:
        if not self.enabled or self._conn is None:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO usage (ts, request_id, endpoint, requested, model, api_key_id, "
                    "prompt_tokens, completion_tokens, total_tokens, latency_ms, status, stream) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        time.time(),
                        request_id,
                        endpoint,
                        requested,
                        model,
                        api_key_id,
                        int(prompt_tokens or 0),
                        int(completion_tokens or 0),
                        int(total_tokens or 0),
                        latency_ms,
                        status,
                        1 if stream else 0,
                    ),
                )
                self._conn.commit()
        except Exception as exc:  # noqa: BLE001 - logging must never break a request
            log.warning("usage log write failed: %s", exc)

    def summary(self, limit: int = 20) -> dict[str, Any]:
        """Aggregate totals + per-model/per-endpoint counts + recent rows."""
        if not self.enabled or self._conn is None:
            return {"enabled": False}
        with self._lock:
            cur = self._conn.cursor()
            total = cur.execute(
                "SELECT COUNT(*), COALESCE(SUM(total_tokens), 0) FROM usage"
            ).fetchone()
            by_model = cur.execute(
                "SELECT COALESCE(model, '?'), COUNT(*), COALESCE(SUM(total_tokens), 0) "
                "FROM usage GROUP BY model ORDER BY 2 DESC"
            ).fetchall()
            by_endpoint = cur.execute(
                "SELECT endpoint, COUNT(*) FROM usage GROUP BY endpoint ORDER BY 2 DESC"
            ).fetchall()
            recent = cur.execute(
                "SELECT ts, endpoint, requested, model, total_tokens, latency_ms, status, stream "
                "FROM usage ORDER BY id DESC LIMIT ?",
                (max(0, int(limit)),),
            ).fetchall()
        return {
            "enabled": True,
            "requests": total[0],
            "total_tokens": total[1],
            "by_model": [{"model": m, "requests": c, "total_tokens": t} for (m, c, t) in by_model],
            "by_endpoint": [{"endpoint": e, "requests": c} for (e, c) in by_endpoint],
            "recent": [
                {
                    "ts": ts,
                    "endpoint": e,
                    "requested": rq,
                    "model": m,
                    "total_tokens": tt,
                    "latency_ms": lm,
                    "status": st,
                    "stream": bool(sr),
                }
                for (ts, e, rq, m, tt, lm, st, sr) in recent
            ],
        }

    def close(self) -> None:
        if self._conn is not None:
            with self._lock:
                try:
                    self._conn.close()
                finally:
                    self._conn = None
