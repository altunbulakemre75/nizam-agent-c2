"""
cop/analytics.py — TimescaleDB analytics query helpers.

All public functions accept an AsyncSession (or None) and return plain
dicts / lists — no SQLAlchemy models in the output so they can be
JSON-serialised directly.

Falls back to date_trunc() on plain PostgreSQL when time_bucket() is
not available (i.e. TimescaleDB extension not installed).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("nizam.analytics")

# ── Bucket helpers ────────────────────────────────────────────────────────────

async def _query(db: AsyncSession, sql: str, params: dict) -> List[Dict[str, Any]]:
    """Run raw SQL and return rows as list-of-dicts."""
    try:
        result = await db.execute(text(sql), params)
        cols = result.keys()
        return [dict(zip(cols, row)) for row in result.fetchall()]
    except Exception as exc:
        log.warning("[analytics] query failed: %s", exc)
        return []


def _time_bucket(interval: str, col: str = "time") -> str:
    """
    Return a time-bucketing SQL expression.
    Try time_bucket first (TimescaleDB); fall back to date_trunc.
    We can't know at call time which is available, so we embed both in a
    CASE-free way: the caller wraps with try/except and retries.
    """
    return f"time_bucket('{interval}', {col})"


def _date_trunc(trunc: str, col: str = "time") -> str:
    return f"date_trunc('{trunc}', {col})"


async def _query_with_fallback(
    db: AsyncSession,
    tsdb_sql: str,
    pg_sql: str,
    params: dict,
) -> List[Dict[str, Any]]:
    """Try TimescaleDB query; fall back to plain Postgres on error."""
    rows = await _query(db, tsdb_sql, params)
    if not rows:
        # Could be empty data OR time_bucket unavailable — try plain PG
        rows = await _query(db, pg_sql, params)
    return rows


# ── Public API ────────────────────────────────────────────────────────────────

async def track_rate(
    db: Optional[AsyncSession],
    hours: int = 24,
    bucket_minutes: int = 5,
) -> List[Dict[str, Any]]:
    """
    Track ingest count per N-minute bucket for the last `hours` hours.
    Returns [{bucket: ISO-string, count: int}, ...]
    """
    if db is None:
        return []
    tsdb = f"""
        SELECT {_time_bucket(f'{bucket_minutes} minutes')} AS bucket,
               COUNT(*) AS count
        FROM   track_events
        WHERE  time > NOW() - INTERVAL ':hours hours'
        GROUP  BY bucket
        ORDER  BY bucket
    """
    pg = f"""
        SELECT {_date_trunc('hour')} AS bucket,
               COUNT(*) AS count
        FROM   track_events
        WHERE  time > NOW() - INTERVAL ':hours hours'
        GROUP  BY bucket
        ORDER  BY bucket
    """
    rows = await _query_with_fallback(db, tsdb, pg, {"hours": hours})
    return [{"bucket": str(r["bucket"]), "count": int(r["count"])} for r in rows]


async def threat_distribution(
    db: Optional[AsyncSession],
    hours: int = 24,
) -> List[Dict[str, Any]]:
    """
    Threat events per hour per threat_level for the last `hours` hours.
    Returns [{bucket, threat_level, count}, ...]
    """
    if db is None:
        return []
    tsdb = f"""
        SELECT {_time_bucket('1 hour')} AS bucket,
               threat_level,
               COUNT(*) AS count
        FROM   threat_events
        WHERE  time > NOW() - INTERVAL ':hours hours'
        GROUP  BY bucket, threat_level
        ORDER  BY bucket
    """
    pg = f"""
        SELECT {_date_trunc('hour')} AS bucket,
               threat_level,
               COUNT(*) AS count
        FROM   threat_events
        WHERE  time > NOW() - INTERVAL ':hours hours'
        GROUP  BY bucket, threat_level
        ORDER  BY bucket
    """
    rows = await _query_with_fallback(db, tsdb, pg, {"hours": hours})
    return [
        {"bucket": str(r["bucket"]), "threat_level": r["threat_level"], "count": int(r["count"])}
        for r in rows
    ]


async def alert_rate(
    db: Optional[AsyncSession],
    hours: int = 24,
) -> List[Dict[str, Any]]:
    """
    Zone breach alert count per hour for the last `hours` hours.
    Returns [{bucket, count}, ...]
    """
    if db is None:
        return []
    tsdb = f"""
        SELECT {_time_bucket('1 hour')} AS bucket,
               COUNT(*) AS count
        FROM   alert_records
        WHERE  time > NOW() - INTERVAL ':hours hours'
        GROUP  BY bucket
        ORDER  BY bucket
    """
    pg = f"""
        SELECT {_date_trunc('hour')} AS bucket,
               COUNT(*) AS count
        FROM   alert_records
        WHERE  time > NOW() - INTERVAL ':hours hours'
        GROUP  BY bucket
        ORDER  BY bucket
    """
    rows = await _query_with_fallback(db, tsdb, pg, {"hours": hours})
    return [{"bucket": str(r["bucket"]), "count": int(r["count"])} for r in rows]


async def audit_summary(
    db: Optional[AsyncSession],
    hours: int = 24,
) -> List[Dict[str, Any]]:
    """
    Audit log action count per hour for the last `hours` hours.
    Returns [{bucket, count}, ...]
    """
    if db is None:
        return []
    tsdb = f"""
        SELECT {_time_bucket('1 hour')} AS bucket,
               COUNT(*) AS count
        FROM   audit_logs
        WHERE  time > NOW() - INTERVAL ':hours hours'
        GROUP  BY bucket
        ORDER  BY bucket
    """
    pg = f"""
        SELECT {_date_trunc('hour')} AS bucket,
               COUNT(*) AS count
        FROM   audit_logs
        WHERE  time > NOW() - INTERVAL ':hours hours'
        GROUP  BY bucket
        ORDER  BY bucket
    """
    rows = await _query_with_fallback(db, tsdb, pg, {"hours": hours})
    return [{"bucket": str(r["bucket"]), "count": int(r["count"])} for r in rows]
