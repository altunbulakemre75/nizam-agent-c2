"""
cop/db_writes.py  —  Fire-and-forget DB persistence helpers

Every POST/DELETE handler that touches STATE also needs to mirror the
change to Postgres (when DB_ENABLED). Before this module existed those
helpers lived at the top of cop/server.py which made any router
extraction pull in the entire DB import surface. Now the helpers live
here and the routers can `from cop.db_writes import db_write, persist_zone, ...`.

All functions are no-ops when the DB is not configured, so test
environments and --no-db pilots keep working unchanged.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

log = logging.getLogger("cop.db_writes")

try:
    from db.session import AsyncSessionLocal
    from db.models import (
        TrackEvent, ThreatEvent, AlertRecord,
        ZoneRecord, AssetRecord, WaypointRecord,
    )
    DB_ENABLED = True
except Exception:
    DB_ENABLED = False
    AsyncSessionLocal = None  # type: ignore


async def db_write(coro) -> None:
    """Await a DB write coroutine, log and swallow errors.

    This wrapper exists so route handlers can `asyncio.create_task(db_write(...))`
    without worrying about whether DB is configured or whether the write fails.
    """
    if not DB_ENABLED:
        coro.close()  # prevent 'coroutine never awaited' RuntimeWarning
        return
    try:
        await coro
    except Exception as exc:
        log.debug("[db] write error: %s", exc)


async def persist_track(payload: Dict[str, Any]) -> None:
    if not DB_ENABLED:
        return
    async with AsyncSessionLocal() as s:
        tid = (payload.get("id") or payload.get("track_id")
               or payload.get("global_track_id") or payload.get("gid"))
        row = TrackEvent(
            track_id=str(tid) if tid else "unknown",
            lat     =payload.get("lat"),
            lon     =payload.get("lon"),
            altitude=payload.get("altitude") or payload.get("alt"),
            speed   =payload.get("speed"),
            heading =payload.get("heading"),
            source  =payload.get("source"),
            raw     =payload,
        )
        s.add(row)
        await s.commit()


async def persist_threat(payload: Dict[str, Any]) -> None:
    if not DB_ENABLED:
        return
    async with AsyncSessionLocal() as s:
        tid = (payload.get("id") or payload.get("track_id")
               or payload.get("global_track_id") or payload.get("gid"))
        row = ThreatEvent(
            track_id    =str(tid) if tid else "unknown",
            threat_level=payload.get("threat_level"),
            intent      =payload.get("intent"),
            score       =payload.get("score"),
            tti_s       =payload.get("tti_s"),
            raw         =payload,
        )
        s.add(row)
        await s.commit()


async def persist_alert(payload: Dict[str, Any]) -> None:
    if not DB_ENABLED:
        return
    async with AsyncSessionLocal() as s:
        row = AlertRecord(
            track_id =payload.get("track_id", ""),
            zone_id  =payload.get("zone_id"),
            zone_name=payload.get("zone_name"),
            zone_type=payload.get("zone_type"),
            lat      =payload.get("lat"),
            lon      =payload.get("lon"),
        )
        s.add(row)
        await s.commit()


async def persist_zone(zone: Dict[str, Any]) -> None:
    if not DB_ENABLED:
        return
    async with AsyncSessionLocal() as s:
        row = ZoneRecord(
            id         =zone["id"],
            name       =zone["name"],
            type       =zone.get("type", "restricted"),
            coordinates=zone["coordinates"],
            color      =zone.get("color"),
        )
        await s.merge(row)
        await s.commit()


async def delete_zone_db(zone_id: str) -> None:
    if not DB_ENABLED:
        return
    from sqlalchemy import delete
    async with AsyncSessionLocal() as s:
        await s.execute(delete(ZoneRecord).where(ZoneRecord.id == zone_id))
        await s.commit()


async def persist_asset(asset: Dict[str, Any]) -> None:
    if not DB_ENABLED:
        return
    async with AsyncSessionLocal() as s:
        row = AssetRecord(
            id    =asset["id"],
            name  =asset["name"],
            type  =asset.get("type", "unknown"),
            lat   =asset["lat"],
            lon   =asset["lon"],
            status=asset.get("status", "active"),
        )
        await s.merge(row)
        await s.commit()


async def delete_asset_db(asset_id: str) -> None:
    if not DB_ENABLED:
        return
    from sqlalchemy import delete
    async with AsyncSessionLocal() as s:
        await s.execute(delete(AssetRecord).where(AssetRecord.id == asset_id))
        await s.commit()


async def persist_waypoint(wp: Dict[str, Any]) -> None:
    if not DB_ENABLED:
        return
    async with AsyncSessionLocal() as s:
        row = WaypointRecord(
            id        =wp["id"],
            name      =wp["name"],
            lat       =wp["lat"],
            lon       =wp["lon"],
            order     =wp.get("order", 0),
            mission_id=wp.get("mission_id", "default"),
        )
        await s.merge(row)
        await s.commit()


async def delete_waypoint_db(wp_id: str) -> None:
    if not DB_ENABLED:
        return
    from sqlalchemy import delete
    async with AsyncSessionLocal() as s:
        await s.execute(delete(WaypointRecord).where(WaypointRecord.id == wp_id))
        await s.commit()


async def clear_waypoints_db() -> None:
    if not DB_ENABLED:
        return
    from sqlalchemy import delete
    async with AsyncSessionLocal() as s:
        await s.execute(delete(WaypointRecord))
        await s.commit()


# Note: persist_task / persist_task_update stay in cop/server.py for now —
# they have field-by-field coupling with the task router which has not
# been extracted yet.
