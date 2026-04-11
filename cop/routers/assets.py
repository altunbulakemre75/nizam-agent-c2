"""cop/routers/assets.py  —  Friendly/hostile asset CRUD + PATCH."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from cop import audit as cop_audit
from cop.db_writes import db_write, persist_asset, delete_asset_db
from cop.helpers import new_id, utc_now_iso
from cop.state import STATE, STATE_LOCK
from cop.ws_broadcast import append_event_tail as _append_event_tail, broadcast

try:
    from auth.deps import require_operator
except Exception:
    def require_operator():
        return lambda: None

router = APIRouter(tags=["assets"])


@router.get("/api/assets")
async def api_assets():
    return JSONResponse({"assets": list(STATE["assets"].values()), "server_time": utc_now_iso()})


@router.post("/api/assets")
async def api_assets_create(req: Request, current_user=Depends(require_operator())):
    body = await req.json()
    if not body.get("lat") or not body.get("lon") or not body.get("type"):
        return JSONResponse({"ok": False, "error": "lat, lon, type required"}, status_code=400)
    asset_id = body.get("id") or new_id("asset-")
    asset = {
        "id":         asset_id,
        "name":       body.get("name", asset_id),
        "type":       body.get("type", "unknown"),
        "lat":        float(body["lat"]),
        "lon":        float(body["lon"]),
        "status":     body.get("status", "active"),
        "created_at": utc_now_iso(),
    }
    async with STATE_LOCK:
        STATE["assets"][asset_id] = asset
    ev = {"event_type": "cop.asset", "payload": asset}
    _append_event_tail(ev)
    await broadcast(ev)
    asyncio.create_task(db_write(persist_asset(asset)))
    asyncio.create_task(cop_audit.log_action(
        username=getattr(current_user, "username", "anonymous"),
        role=getattr(current_user, "role", ""),
        action="CREATE_ASSET", resource_type="asset", resource_id=asset_id,
        detail={"type": asset.get("type"), "name": asset.get("name")},
        ip=req.client.host if req.client else "",
    ))
    return JSONResponse({"ok": True, "asset": asset})


@router.delete("/api/assets/{asset_id}")
async def api_assets_delete(asset_id: str, req: Request, current_user=Depends(require_operator())):
    async with STATE_LOCK:
        removed = STATE["assets"].pop(asset_id, None)
    if not removed:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    ev = {"event_type": "cop.asset_removed", "payload": {"id": asset_id}}
    _append_event_tail(ev)
    await broadcast(ev)
    asyncio.create_task(db_write(delete_asset_db(asset_id)))
    asyncio.create_task(cop_audit.log_action(
        username=getattr(current_user, "username", "anonymous"),
        role=getattr(current_user, "role", ""),
        action="DELETE_ASSET", resource_type="asset", resource_id=asset_id,
        ip=req.client.host if req.client else "",
    ))
    return JSONResponse({"ok": True, "removed": asset_id})


@router.patch("/api/assets/{asset_id}")
async def api_asset_patch(
    asset_id: str, req: Request,
    current_user=Depends(require_operator()),
):
    """Partial update of an asset record (status, lat, lon, name, capability, range_km)."""
    body = await req.json()
    allowed = {"status", "lat", "lon", "name", "capability", "range_km"}
    async with STATE_LOCK:
        asset = STATE["assets"].get(asset_id)
        if not asset:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        for k, v in body.items():
            if k in allowed:
                asset[k] = v
    await broadcast({"event_type": "cop.asset", "payload": dict(asset)})
    asyncio.create_task(cop_audit.log_action(
        username=getattr(current_user, "username", "operator"),
        role=getattr(current_user, "role", ""),
        action="PATCH_ASSET", resource_type="asset", resource_id=asset_id,
        detail={k: v for k, v in body.items() if k in allowed},
        ip=req.client.host if req.client else "",
    ))
    return JSONResponse({"ok": True, "asset": asset})
