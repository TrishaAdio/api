"""SQLite-backed usage log.

Every chat request is recorded with the caller's IP, the model that served it,
token estimates, the model's credit multiplier and latency. Writes happen in a
worker thread so the event loop is never blocked on disk I/O.
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import os
import secrets
import sqlite3
import time
from typing import Any, Dict, List, Optional

from .config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL    NOT NULL,
    ip                TEXT    NOT NULL DEFAULT '',
    model             TEXT    NOT NULL DEFAULT '',
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_multiplier   REAL    NOT NULL DEFAULT 0,
    credits           REAL    NOT NULL DEFAULT 0,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    status            INTEGER NOT NULL DEFAULT 200,
    stream            INTEGER NOT NULL DEFAULT 0,
    source            TEXT    NOT NULL DEFAULT 'api',
    user_agent        TEXT    NOT NULL DEFAULT '',
    error             TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts DESC);
CREATE INDEX IF NOT EXISTS idx_usage_model ON usage(model);
CREATE INDEX IF NOT EXISTS idx_usage_ip ON usage(ip);

CREATE TABLE IF NOT EXISTS api_keys (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash   TEXT    NOT NULL UNIQUE,
    prefix     TEXT    NOT NULL DEFAULT '',
    name       TEXT    NOT NULL DEFAULT '',
    created_at REAL    NOT NULL,
    created_by TEXT    NOT NULL DEFAULT '',
    last_used  REAL,
    last_ip    TEXT    NOT NULL DEFAULT '',
    requests   INTEGER NOT NULL DEFAULT 0,
    revoked    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_keys_hash ON api_keys(key_hash);

CREATE TABLE IF NOT EXISTS ip_whitelist (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ip         TEXT    NOT NULL UNIQUE,
    label      TEXT    NOT NULL DEFAULT '',
    created_at REAL    NOT NULL,
    added_by   TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS usage_keys (
    usage_id INTEGER PRIMARY KEY,
    key_id   INTEGER
);
"""


def _connect() -> sqlite3.Connection:
    directory = os.path.dirname(os.path.abspath(settings.usage_db_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(settings.usage_db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL keeps readers from blocking the writer while the dashboard polls.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init_sync() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def _record_sync(row: Dict[str, Any], key_id: Optional[int]) -> int:
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO usage (ts, ip, model, prompt_tokens, completion_tokens,
                               cost_multiplier, credits, latency_ms, status,
                               stream, source, user_agent, error)
            VALUES (:ts, :ip, :model, :prompt_tokens, :completion_tokens,
                    :cost_multiplier, :credits, :latency_ms, :status,
                    :stream, :source, :user_agent, :error)
            """,
            row,
        )
        usage_id = cursor.lastrowid
        if key_id is not None:
            conn.execute(
                "INSERT OR REPLACE INTO usage_keys (usage_id, key_id) VALUES (?, ?)",
                (usage_id, key_id),
            )
    return usage_id


def _recent_sync(limit: int, model: Optional[str], ip: Optional[str]) -> List[Dict[str, Any]]:
    clauses, params = [], []
    if model:
        clauses.append("model = ?")
        params.append(model)
    if ip:
        clauses.append("ip = ?")
        params.append(ip)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, min(limit, 1000)))

    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM usage {0} ORDER BY ts DESC LIMIT ?".format(where), params
        ).fetchall()
    return [dict(r) for r in rows]


def _stats_sync() -> Dict[str, Any]:
    now = time.time()
    day_ago = now - 86400

    with _connect() as conn:
        totals = conn.execute(
            """
            SELECT COUNT(*) AS requests,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(credits), 0) AS credits,
                   COUNT(DISTINCT ip) AS unique_ips,
                   COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                   COALESCE(SUM(status >= 400), 0) AS errors
            FROM usage
            """
        ).fetchone()

        today = conn.execute(
            """
            SELECT COUNT(*) AS requests,
                   COALESCE(SUM(credits), 0) AS credits,
                   COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens
            FROM usage WHERE ts >= ?
            """,
            (day_ago,),
        ).fetchone()

        # Kiro allowances reset monthly, so the billing period is the current
        # calendar month in UTC.
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        period_start = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = (period_start + datetime.timedelta(days=32)).replace(day=1)

        period = conn.execute(
            """
            SELECT COUNT(*) AS requests,
                   COALESCE(SUM(credits), 0) AS credits
            FROM usage WHERE ts >= ?
            """,
            (period_start.timestamp(),),
        ).fetchone()

        by_model = conn.execute(
            """
            SELECT model,
                   COUNT(*) AS requests,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(credits), 0) AS credits
            FROM usage GROUP BY model ORDER BY requests DESC
            """
        ).fetchall()

        by_ip = conn.execute(
            """
            SELECT ip,
                   COUNT(*) AS requests,
                   COALESCE(SUM(credits), 0) AS credits,
                   COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,
                   MAX(ts) AS last_seen
            FROM usage GROUP BY ip ORDER BY requests DESC LIMIT 50
            """
        ).fetchall()

        # 24 hourly buckets, oldest first, zero-filled.
        series_rows = conn.execute(
            """
            SELECT CAST((? - ts) / 3600 AS INTEGER) AS bucket,
                   COUNT(*) AS requests,
                   COALESCE(SUM(credits), 0) AS credits
            FROM usage WHERE ts >= ? GROUP BY bucket
            """,
            (now, day_ago),
        ).fetchall()

    buckets = {int(r["bucket"]): r for r in series_rows}
    series = []
    for offset in range(23, -1, -1):
        row = buckets.get(offset)
        series.append(
            {
                "requests": row["requests"] if row else 0,
                "credits": round(row["credits"], 4) if row else 0.0,
            }
        )

    return {
        "totals": dict(totals),
        "last_24h": dict(today),
        "period": {
            "requests": period["requests"],
            "credits": round(period["credits"], 6),
            "start": period_start.timestamp(),
            "reset": next_month.timestamp(),
            "label": period_start.strftime("%B %Y"),
        },
        "by_model": [dict(r) for r in by_model],
        "by_ip": [dict(r) for r in by_ip],
        "series": series,
    }


def _purge_sync() -> int:
    with _connect() as conn:
        deleted = conn.execute("DELETE FROM usage").rowcount
    return deleted


async def _run(fn, *args):
    return await asyncio.get_event_loop().run_in_executor(None, fn, *args)


async def init() -> None:
    await _run(_init_sync)


async def record(
    ip: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_multiplier: float = 0.0,
    latency_ms: int = 0,
    status: int = 200,
    stream: bool = False,
    source: str = "api",
    user_agent: str = "",
    error: Optional[str] = None,
    key_id: Optional[int] = None,
) -> None:
    """Persist one request. Never raises: logging must not break a response."""
    row = {
        "ts": time.time(),
        "ip": ip or "",
        "model": model or "",
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost_multiplier": cost_multiplier,
        # Kiro bills per request, scaled by the model's credit multiplier, not
        # per token. One completed request therefore costs `cost_multiplier`
        # credits. Failed requests are recorded at zero.
        "credits": round(cost_multiplier, 6) if status < 400 else 0.0,
        "latency_ms": latency_ms,
        "status": status,
        "stream": 1 if stream else 0,
        "source": source,
        "user_agent": (user_agent or "")[:200],
        "error": error,
    }
    try:
        await _run(_record_sync, row, key_id)
    except Exception:  # noqa: BLE001 - logging is best-effort
        pass


async def recent(limit: int = 100, model: Optional[str] = None, ip: Optional[str] = None):
    return await _run(_recent_sync, limit, model, ip)


# ═══ API keys ═════════════════════════════════════════════════════════════
#
# Only the SHA-256 of each key is stored, so a database leak does not expose
# usable credentials. The plaintext is shown once at creation and never again.

_KEY_PREFIX = "sk-rio-"


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _create_key_sync(name: str, created_by: str) -> Dict[str, Any]:
    raw = _KEY_PREFIX + secrets.token_hex(24)
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO api_keys (key_hash, prefix, name, created_at, created_by)
            VALUES (?, ?, ?, ?, ?)
            """,
            (_hash_key(raw), raw[: len(_KEY_PREFIX) + 6], name, time.time(), created_by),
        )
        key_id = cursor.lastrowid
    return {"id": key_id, "key": raw, "name": name}


def _list_keys_sync() -> List[Dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT k.id, k.prefix, k.name, k.created_at, k.created_by,
                   k.last_used, k.last_ip, k.requests, k.revoked,
                   COALESCE((SELECT SUM(u.credits) FROM usage u
                             JOIN usage_keys uk ON uk.usage_id = u.id
                             WHERE uk.key_id = k.id), 0) AS credits
            FROM api_keys k ORDER BY k.created_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def _verify_key_sync(raw: str) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, name, revoked FROM api_keys WHERE key_hash = ?", (_hash_key(raw),)
        ).fetchone()
    if row is None or row["revoked"]:
        return None
    return dict(row)


def _touch_key_sync(key_id: int, ip: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE api_keys SET last_used = ?, last_ip = ?, requests = requests + 1 WHERE id = ?",
            (time.time(), ip, key_id),
        )


def _revoke_key_sync(key_id: int) -> bool:
    with _connect() as conn:
        changed = conn.execute("UPDATE api_keys SET revoked = 1 WHERE id = ?", (key_id,)).rowcount
    return bool(changed)


def _delete_key_sync(key_id: int) -> bool:
    with _connect() as conn:
        changed = conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,)).rowcount
    return bool(changed)


def _attach_key_sync(usage_id: int, key_id: Optional[int]) -> None:
    if key_id is None:
        return
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO usage_keys (usage_id, key_id) VALUES (?, ?)",
            (usage_id, key_id),
        )


async def create_key(name: str = "", created_by: str = "") -> Dict[str, Any]:
    return await _run(_create_key_sync, name, created_by)


async def list_keys() -> List[Dict[str, Any]]:
    return await _run(_list_keys_sync)


async def verify_key(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    return await _run(_verify_key_sync, raw)


async def touch_key(key_id: int, ip: str = "") -> None:
    try:
        await _run(_touch_key_sync, key_id, ip)
    except Exception:  # noqa: BLE001
        pass


async def revoke_key(key_id: int) -> bool:
    return await _run(_revoke_key_sync, key_id)


async def delete_key(key_id: int) -> bool:
    return await _run(_delete_key_sync, key_id)


# ═══ IP whitelist ═════════════════════════════════════════════════════════


def _list_whitelist_sync() -> List[Dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM ip_whitelist ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def _add_whitelist_sync(ip: str, label: str, added_by: str) -> bool:
    with _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO ip_whitelist (ip, label, created_at, added_by) VALUES (?, ?, ?, ?)",
                (ip, label, time.time(), added_by),
            )
        except sqlite3.IntegrityError:
            return False
    return True


def _remove_whitelist_sync(ip: str) -> bool:
    with _connect() as conn:
        changed = conn.execute("DELETE FROM ip_whitelist WHERE ip = ?", (ip,)).rowcount
    return bool(changed)


async def list_whitelist() -> List[Dict[str, Any]]:
    return await _run(_list_whitelist_sync)


async def add_whitelist(ip: str, label: str = "", added_by: str = "") -> bool:
    return await _run(_add_whitelist_sync, ip, label, added_by)


async def remove_whitelist(ip: str) -> bool:
    return await _run(_remove_whitelist_sync, ip)


async def stats() -> Dict[str, Any]:
    return await _run(_stats_sync)


async def purge() -> int:
    return await _run(_purge_sync)
