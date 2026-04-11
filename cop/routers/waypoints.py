"""cop/routers/waypoints.py  —  Mission planning waypoint CRUD."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from cop.db_writes import db_write, persist_waypoint, delete_waypoint_db, clear_waypoints_db
from cop.helpers import new_id, utc_now_iso
from cop.state import STATE, STATE_LOCK
from cop.ws_broadcast import append_event_tail as _append_event_tail, broadcast

try:
    from auth.deps import require_operator
except Exception:
    def require_operator():
        return lambda: None

router = APIRouter(tags=["waypoints"])


@router.get("/api/waypoints")
async def api_waypoints():
    wps = sorted(STATE["waypoints"].values(), key=lambda w: w.get("order", 0))
    return JSONResponse({"waypoints": wps, "server_time": utc_now_iso()})


@router.post("/api/waypoints")
async def api_waypoints_create(req: Request, _=Depends(require_operator())):
    body = await req.json()
    if body.get("lat") is None or body.get("lon") is None:
        return JSONResponse({"ok": False, "error": "lat and lon required"}, status_code=400)
    wp_id = body.get("id") or new_id("wp-")
    wp = {
        "id":         wp_id,
        "name":       body.get("name", f"WP-{len(STATE['waypoints']) + 1}"),
        "lat":        float(body["lat"]),
        "lon":        float(body["lon"]),
        "order":      int(body.get("order", len(STATE["waypoints"]))),
        "mission_id": body.get("mission_id", "default"),
        "created_at": utc_now_iso(),
    }
    async with STATE_LOCK:
        STATE["waypoints"][wp_id] = wp
    ev = {"event_type": "cop.waypoint", "payload": wp}
    _append_event_tail(ev)
    await broadcast(ev)
    asyncio.create_task(db_write(persist_waypoint(wp)))
    return JSONResponse({"ok": True, "waypoint": wp})


@router.delete("/api/waypoints/{wp_id}")
async def api_waypoints_delete(wp_id: str, _=Depends(require_operator())):
    async with STATE_LOCK:
        removed = STATE["waypoints"].pop(wp_id, None)
    if not removed:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    ev = {"event_type": "cop.waypoint_removed", "payload": {"id": wp_id}}
    _append_event_tail(ev)
    await broadcast(ev)
    asyncio.create_task(db_write(delete_waypoint_db(wp_id)))
    return JSONResponse({"ok": True, "removed": wp_id})


@router.delete("/api/waypoints")
async def api_waypoints_clear(_=Depends(require_operator())):
    async with STATE_LOCK:
        STATE["waypoints"].clear()
    ev = {"event_type": "cop.waypoints_cleared", "payload": {"server_time": utc_now_iso()}}
    _append_event_tail(ev)
    await broadcast(ev)
    asyncio.create_task(db_write(clear_waypoints_db()))
    return JSONResponse({"ok": True})
