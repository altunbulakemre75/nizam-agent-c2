"""
db/init_db.py — Creates all tables and TimescaleDB hypertables.

Call on COP server startup when DATABASE_URL is set.
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from db.models import Base

log = logging.getLogger("nizam.db")

# Tables that become TimescaleDB hypertables (partitioned by 'time')
HYPERTABLES = [
    "track_events",
    "threat_events",
    "alert_records",
]


async def init_db(engine: AsyncEngine) -> None:
    """Create tables and promote time-series tables to hypertables."""
    log.info("[db] Creating tables...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Attempt TimescaleDB hypertable promotion (skip if extension not installed)
    async with engine.begin() as conn:
        for table in HYPERTABLES:
            try:
                await conn.execute(
                    text(
                        f"SELECT create_hypertable('{table}', 'time', "
                        f"if_not_exists => TRUE);"
                    )
                )
                log.info("[db] Hypertable ready: %s", table)
            except Exception as exc:
                # Plain PostgreSQL without TimescaleDB — table already created,
                # just skip hypertable promotion
                log.warning(
                    "[db] Hypertable skip (%s): %s — running as plain Postgres",
                    table,
                    exc,
                )

    log.info("[db] Database ready.")
