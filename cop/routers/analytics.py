"""cop/routers/analytics.py  —  Historical analytics read endpoints.

NOTE on duplicate routes: prior to this extraction, cop/server.py had two
sets of `/api/analytics/*` endpoints — one live (track_id-filtered DB
reads) and one shadowed by route-order (hours-binned summaries via
cop.analytics). The shadowed set was unreachable dead code. This module
keeps only the live set.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

try:
    from db.session import AsyncSessionLocal
    from db.models import TrackEvent, ThreatEvent, AlertRecord
    DB_ENABLED = True
except Exception:
    DB_ENABLED = False

router = APIRouter(tags=["analytics"])


@router.get("/api/analytics/tracks")
async def api_analytics_tracks(
    track_id: Optional[str] = Query(None),
    limit:    int           = Query(100, le=5000),
):
    """Query track history from DB. Returns 503 when DB not configured."""
    if not DB_ENABLED:
        return JSONResponse({"ok": False, "error": "database not configured"}, status_code=503)
    from sqlalchemy import select, desc
    async with AsyncSessionLocal() as s:
        q = select(TrackEvent).order_by(desc(TrackEvent.time)).limit(limit)
        if track_id:
            q = q.where(TrackEvent.track_id == track_id)
        rows = (await s.execute(q)).scalars().all()
    return JSONResponse({
        "count": len(rows),
        "tracks": [
            {
                "time": r.time.isoformat(), "track_id": r.track_id,
                "lat": r.lat, "lon": r.lon, "altitude": r.altitude,
                "speed": r.speed, "heading": r.heading, "source": r.source,
            }
            for r in rows
        ],
    })


@router.get("/api/analytics/threats")
async def api_analytics_threats(
    track_id: Optional[str] = Query(None),
    limit:    int           = Query(100, le=5000),
):
    if not DB_ENABLED:
        return JSONResponse({"ok": False, "error": "database not configured"}, status_code=503)
    from sqlalchemy import select, desc
    async with AsyncSessionLocal() as s:
        q = select(ThreatEvent).order_by(desc(ThreatEvent.time)).limit(limit)
        if track_id:
            q = q.where(ThreatEvent.track_id == track_id)
        rows = (await s.execute(q)).scalars().all()
    return JSONResponse({
        "count": len(rows),
        "threats": [
            {
                "time": r.time.isoformat(), "track_id": r.track_id,
                "threat_level": r.threat_level, "intent": r.intent,
                "score": r.score, "tti_s": r.tti_s,
            }
            for r in rows
        ],
    })


@router.get("/api/analytics/alerts")
async def api_analytics_alerts(limit: int = Query(100, le=5000)):
    if not DB_ENABLED:
        return JSONResponse({"ok": False, "error": "database not configured"}, status_code=503)
    from sqlalchemy import select, desc
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(AlertRecord).order_by(desc(AlertRecord.time)).limit(limit)
        )).scalars().all()
    return JSONResponse({
        "count": len(rows),
        "alerts": [
            {
                "time": r.time.isoformat(), "track_id": r.track_id,
                "zone_id": r.zone_id, "zone_name": r.zone_name,
                "zone_type": r.zone_type, "lat": r.lat, "lon": r.lon,
            }
            for r in rows
        ],
    })
