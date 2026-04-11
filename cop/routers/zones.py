"""cop/routers/zones.py  —  Zone CRUD (restricted / kill / safe polygons)."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from cop import audit as cop_audit
from cop.db_writes import db_write, persist_zone, delete_zone_db
from cop.helpers import utc_now_iso
from cop.state import STATE, STATE_LOCK
from cop.ws_broadcast import append_event_tail as _append_event_tail, broadcast

try:
    from auth.deps import require_operator
except Exception:
    def require_operator():
        return lambda: None

router = APIRouter(tags=["zones"])


@router.get("/api/zones")
async def api_zones():
    return JSONResponse({"zones": list(STATE["zones"].values()), "server_time": utc_now_iso()})


@router.post("/api/zones")
async def api_zones_create(req: Request, current_user=Depends(require_operator())):
    body = await req.json()
    zone_id = body.get("id")
    if not zone_id or not body.get("coordinates"):
        return JSONResponse({"ok": False, "error": "id and coordinates required"}, status_code=400)
    zone = {
        "id":          zone_id,
        "name":        body.get("name", zone_id),
        "type":        body.get("type", "restricted"),
        "coordinates": body["coordinates"],
        "color":       body.get("color"),
        "created_at":  utc_now_iso(),
    }
    async with STATE_LOCK:
        STATE["zones"][zone_id] = zone
    ev = {"event_type": "cop.zone", "payload": zone}
    _append_event_tail(ev)
    await broadcast(ev)
    asyncio.create_task(db_write(persist_zone(zone)))
    asyncio.create_task(cop_audit.log_action(
        username=getattr(current_user, "username", "anonymous"),
        role=getattr(current_user, "role", ""),
        action="CREATE_ZONE", resource_type="zone", resource_id=zone_id,
        detail={"name": zone.get("name"), "type": zone.get("type")},
        ip=req.client.host if req.client else "",
    ))
    return JSONResponse({"ok": True, "zone": zone})


@router.delete("/api/zones/{zone_id}")
async def api_zones_delete(zone_id: str, req: Request, current_user=Depends(require_operator())):
    async with STATE_LOCK:
        removed = STATE["zones"].pop(zone_id, None)
    if not removed:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    ev = {"event_type": "cop.zone_removed", "payload": {"id": zone_id}}
    _append_event_tail(ev)
    await broadcast(ev)
    asyncio.create_task(db_write(delete_zone_db(zone_id)))
    asyncio.create_task(cop_audit.log_action(
        username=getattr(current_user, "username", "anonymous"),
        role=getattr(current_user, "role", ""),
        action="DELETE_ZONE", resource_type="zone", resource_id=zone_id,
        ip=req.client.host if req.client else "",
    ))
    return JSONResponse({"ok": True, "removed": zone_id})
