"""cop/routers/sync.py  —  Multi-node peer sync endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from cop import sync as cop_sync
from cop.helpers import utc_now_iso
from cop.state import STATE, STATE_LOCK
from cop.ws_broadcast import broadcast

try:
    from auth.deps import require_operator
except Exception:
    def require_operator():
        return lambda: None

log = logging.getLogger("cop.routers.sync")
router = APIRouter(tags=["sync"])


@router.post("/api/sync/peers")
async def api_sync_add_peer(req: Request, _=Depends(require_operator())):
    """Register a peer COP node URL for state synchronisation."""
    body = await req.json()
    url = body.get("url", "").strip()
    if not url:
        return JSONResponse({"ok": False, "error": "url required"}, status_code=400)
    action = body.get("action", "add")
    if action == "remove":
        removed = cop_sync.remove_peer(url)
        return JSONResponse({"ok": True, "removed": removed})
    cop_sync.add_peer(url)
    return JSONResponse({"ok": True, "peers": cop_sync.list_peers()})


@router.get("/api/sync/status")
async def api_sync_status():
    """Return current peer sync status."""
    return JSONResponse(cop_sync.stats())


@router.post("/api/sync/receive")
async def api_sync_receive(req: Request):
    """Receive a delta snapshot from a peer COP node (last-write-wins merge)."""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    delta = body.get("delta")
    if not isinstance(delta, dict):
        return JSONResponse({"ok": False, "error": "missing delta"}, status_code=400)

    node_id   = body.get("node_id", "unknown")
    pushed_at = body.get("pushed_at", "")

    async with STATE_LOCK:
        applied = cop_sync.apply_delta(delta, STATE, source_node=node_id)

    total_applied = sum(applied.values())
    if total_applied > 0:
        await broadcast({
            "event_type": "cop.sync_applied",
            "payload": {
                "from_node":    node_id,
                "pushed_at":    pushed_at,
                "applied":      applied,
                "server_time":  utc_now_iso(),
            },
        })
        log.info("[sync] applied %d records from %s", total_applied, node_id)

    return JSONResponse({"ok": True, "applied": applied})


@router.get("/api/sync/conflicts")
async def api_sync_conflicts():
    """Return the vector clock conflict log for operator review."""
    return JSONResponse({
        "node_id":    cop_sync.NODE_ID,
        "conflicts":  cop_sync.get_conflicts(),
        "count":      len(cop_sync.get_conflicts()),
    })


@router.delete("/api/sync/conflicts")
async def api_sync_clear_conflicts(_=Depends(require_operator())):
    """Clear the conflict log."""
    n = cop_sync.clear_conflicts()
    return JSONResponse({"ok": True, "cleared": n})
