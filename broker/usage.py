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
from pathlib import Path
import time
from typing import Any

log = logging.getLogger("broker.usage")


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile of a pre-sorted, non-empty list (q in [0,1])."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = int(round(q * (len(sorted_vals) - 1)))
    idx = max(0, min(idx, len(sorted_vals) - 1))
    return sorted_vals[idx]


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
  ttft_ms REAL,
  status TEXT,
  stream INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);
CREATE INDEX IF NOT EXISTS idx_usage_model ON usage(model);
"""

# Columns added after the original ADR 0007 schema. Each is applied with an
# idempotent ``ALTER TABLE ... ADD COLUMN`` only when the column is absent, so an
# existing ``usage.sqlite3`` written by an older broker is migrated in place with
# no data loss and old rows simply carry NULL for the new column.
_MIGRATIONS: tuple[tuple[str, str], ...] = (
    # ADR 0019: time-to-first-token (ms) for streaming requests.
    ("ttft_ms", "ALTER TABLE usage ADD COLUMN ttft_ms REAL"),
)


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
            if self.path != ":memory:" and not self.path.startswith("file:"):
                # The default lives under BROKER_CACHE_DIR (/var/cache/broker in the
                # image); create the parent so a fresh local checkout works too.
                Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.executescript(_SCHEMA)
            self._migrate(self._conn)
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001 - never block startup on the log
            log.warning(
                "usage log init failed for %r (%s): disabling; set BROKER_CACHE_DIR to a "
                "writable directory or BROKER_USAGE_LOG_PATH to a writable file",
                self.path,
                exc,
            )
            self.enabled = False
            self._conn = None

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Apply forward-only, additive column migrations to an existing table.

        Backward compatible: a usage DB written by an older broker (without the
        newer columns) is upgraded in place by adding any missing column. Old
        rows keep NULL for the new column; no row is rewritten or dropped.
        """
        cols = {row[1] for row in conn.execute("PRAGMA table_info(usage)").fetchall()}
        for name, ddl in _MIGRATIONS:
            if name not in cols:
                conn.execute(ddl)

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
        ttft_ms: float | None = None,
    ) -> None:
        if not self.enabled or self._conn is None:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO usage (ts, request_id, endpoint, requested, model, api_key_id, "
                    "prompt_tokens, completion_tokens, total_tokens, latency_ms, status, stream, "
                    "ttft_ms) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                        ttft_ms,
                    ),
                )
                self._conn.commit()
        except Exception as exc:  # noqa: BLE001 - logging must never break a request
            log.warning("usage log write failed: %s", exc)

    def summary(
        self,
        limit: int = 20,
        *,
        api_key_id: str | None = None,
        include_keys: bool = False,
    ) -> dict[str, Any]:
        """Aggregate totals + per-model/per-endpoint counts + recent rows.

        ``api_key_id`` scopes every aggregate to one key (user-scope callers see
        only their own usage). ``include_keys`` adds a per-key spend breakdown
        (admin-scope callers see all keys). Both privacy rules from ADR 0007 are
        preserved: only metadata + token counts are returned, never content, and
        the key identifier is the stored non-secret id, never a secret.
        """
        if not self.enabled or self._conn is None:
            return {"enabled": False}
        where = ""
        params: tuple = ()
        if api_key_id is not None:
            where = " WHERE api_key_id = ?"
            params = (api_key_id,)
        with self._lock:
            cur = self._conn.cursor()
            total = cur.execute(
                f"SELECT COUNT(*), COALESCE(SUM(total_tokens), 0) FROM usage{where}", params
            ).fetchone()
            by_model = cur.execute(
                "SELECT COALESCE(model, '?'), COUNT(*), COALESCE(SUM(total_tokens), 0) "
                f"FROM usage{where} GROUP BY model ORDER BY 2 DESC",
                params,
            ).fetchall()
            by_endpoint = cur.execute(
                f"SELECT endpoint, COUNT(*) FROM usage{where} GROUP BY endpoint ORDER BY 2 DESC",
                params,
            ).fetchall()
            recent = cur.execute(
                "SELECT ts, endpoint, requested, model, total_tokens, latency_ms, status, stream "
                f"FROM usage{where} ORDER BY id DESC LIMIT ?",
                (*params, max(0, int(limit))),
            ).fetchall()
            per_key = None
            if include_keys:
                per_key = cur.execute(
                    "SELECT COALESCE(api_key_id, '?'), COUNT(*), COALESCE(SUM(total_tokens), 0) "
                    "FROM usage GROUP BY api_key_id ORDER BY 3 DESC"
                ).fetchall()
        out: dict[str, Any] = {
            "enabled": True,
            "scope": "key" if api_key_id is not None else "all",
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
        if api_key_id is not None:
            out["api_key_id"] = api_key_id
        if per_key is not None:
            out["by_key"] = [
                {"api_key_id": k, "requests": c, "total_tokens": t} for (k, c, t) in per_key
            ]
        return out

    def latency_percentiles(
        self,
        window_seconds: float = 3600.0,
        endpoints: tuple[str, ...] | None = None,
        metric: str = "latency",
    ) -> dict[str, dict[str, float]]:
        """Per-served-model latency p50/p90 (ms) over a recent window.

        Used by the auto-router's latency-budget feature (ADR 0011/0019).
        ``metric`` selects which timing column is summarized: ``"latency"``
        (end-to-end, the default) or ``"ttft"`` (time-to-first-token, recorded
        for streaming requests per ADR 0019). For ``"ttft"`` only rows that
        carry a non-NULL ``ttft_ms`` are counted, so a model with no streamed
        samples yet returns no entry (and the budget cannot rank it) rather than
        a misleading zero. Best-effort: returns an empty dict if the log is
        disabled or a query fails, so it can never break routing. ``endpoints``
        optionally restricts the rows considered (e.g. non-streaming chat only).
        """
        if not self.enabled or self._conn is None:
            return {}
        col = "ttft_ms" if metric == "ttft" else "latency_ms"
        cutoff = time.time() - max(0.0, float(window_seconds))
        try:
            with self._lock:
                cur = self._conn.cursor()
                sql = (
                    f"SELECT model, {col} FROM usage "
                    f"WHERE ts >= ? AND {col} IS NOT NULL AND model IS NOT NULL"
                )
                params: list[Any] = [cutoff]
                if endpoints:
                    placeholders = ",".join("?" for _ in endpoints)
                    sql += f" AND endpoint IN ({placeholders})"
                    params.extend(endpoints)
                rows = cur.execute(sql, params).fetchall()
        except Exception as exc:  # noqa: BLE001 - never break routing on the log
            log.warning("usage latency query failed: %s", exc)
            return {}
        buckets: dict[str, list[float]] = {}
        for model, lm in rows:
            if lm is None:
                continue
            buckets.setdefault(model, []).append(float(lm))
        out: dict[str, dict[str, float]] = {}
        for model, vals in buckets.items():
            vals.sort()
            out[model] = {
                "p50": _percentile(vals, 0.50),
                "p90": _percentile(vals, 0.90),
                "count": float(len(vals)),
            }
        return out

    def close(self) -> None:
        if self._conn is not None:
            with self._lock:
                try:
                    self._conn.close()
                finally:
                    self._conn = None
