"""cop/routers/operators.py  —  Multi-operator listing + track claim + annotations."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ai import lineage as ai_lineage
from cop import audit as cop_audit
from cop.helpers import new_id, utc_now_iso
from cop.state import (
    STATE,
    STATE_LOCK,
    CLIENTS_LOCK,
    OPERATORS,
    TRACK_CLAIMS,
)
from cop.ws_broadcast import append_event_tail as _append_event_tail, broadcast

try:
    from auth.deps import require_operator
except Exception:
    def require_operator():
        return lambda: None

router = APIRouter(tags=["operators"])


# ── Operator listing ───────────────────────────────────────────────────────
@router.get("/api/operators")
async def api_operators():
    """List active operator sessions and their claimed tracks."""
    async with CLIENTS_LOCK:
        active = {
            op_id: {
                "operator_id": op_id,
                "joined_at":   info["joined_at"],
                "claimed_tracks": [tid for tid, oid in TRACK_CLAIMS.items() if oid == op_id],
            }
            for op_id, info in OPERATORS.items()
        }
    return JSONResponse({"operators": list(active.values()), "claims": dict(TRACK_CLAIMS)})


# ── Track claim / release ──────────────────────────────────────────────────
@router.post("/api/tracks/{track_id}/claim")
async def api_track_claim(track_id: str, req: Request, _=Depends(require_operator())):
    """Claim a track for exclusive task handling. Returns 409 if already claimed."""
    body = await req.json() if req.headers.get("content-length", "0") != "0" else {}
    operator_id = body.get("operator_id", "operator")

    async with CLIENTS_LOCK:
        existing = TRACK_CLAIMS.get(track_id)
        if existing and existing != operator_id:
            return JSONResponse(
                {"ok": False, "error": f"Track already claimed by {existing}"},
                status_code=409,
            )
        TRACK_CLAIMS[track_id] = operator_id

    ev = {
        "event_type": "cop.track_claimed",
        "payload": {
            "track_id":    track_id,
            "operator_id": operator_id,
            "server_time": utc_now_iso(),
        },
    }
    await broadcast(ev)
    try:
        ai_lineage.record(
            track_id=track_id,
            stage="operator",
            summary=f"Track claimed by {operator_id}",
            outputs={"operator_id": operator_id},
            rule="multi_operator.claim",
        )
    except Exception:
        pass
    return JSONResponse({"ok": True, "track_id": track_id, "operator_id": operator_id})


@router.delete("/api/tracks/{track_id}/claim")
async def api_track_release(track_id: str, req: Request, _=Depends(require_operator())):
    """Release a track claim. Only the owning operator can release."""
    body = await req.json() if req.headers.get("content-length", "0") != "0" else {}
    operator_id = body.get("operator_id", "operator")

    async with CLIENTS_LOCK:
        existing = TRACK_CLAIMS.get(track_id)
        if not existing:
            return JSONResponse({"ok": False, "error": "not claimed"}, status_code=404)
        if existing != operator_id:
            return JSONResponse(
                {"ok": False, "error": f"Claim owned by {existing}, not {operator_id}"},
                status_code=403,
            )
        del TRACK_CLAIMS[track_id]

    ev = {
        "event_type": "cop.track_released",
        "payload": {
            "track_id":    track_id,
            "operator_id": operator_id,
            "server_time": utc_now_iso(),
        },
    }
    await broadcast(ev)
    return JSONResponse({"ok": True, "track_id": track_id})


# ── Track annotations ──────────────────────────────────────────────────────
@router.get("/api/tracks/{track_id}/annotations")
async def api_annotations_get(track_id: str):
    """Return all annotations for a track."""
    return JSONResponse({"annotations": STATE["annotations"].get(track_id, [])})


@router.post("/api/tracks/{track_id}/annotations")
async def api_annotations_create(
    track_id: str, req: Request, current_user=Depends(require_operator()),
):
    """Add an operator annotation to a track."""
    body = await req.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "text required"}, status_code=400)

    annotation = {
        "id":         new_id("ann-"),
        "track_id":   track_id,
        "text":       text[:500],
        "author":     getattr(current_user, "username", "anonymous"),
        "created_at": utc_now_iso(),
    }
    async with STATE_LOCK:
        STATE["annotations"].setdefault(track_id, []).append(annotation)

    ev = {"event_type": "cop.annotation", "payload": annotation}
    _append_event_tail(ev)
    await broadcast(ev)
    asyncio.create_task(cop_audit.log_action(
        username=annotation["author"],
        role=getattr(current_user, "role", ""),
        action="CREATE_ANNOTATION", resource_type="track", resource_id=track_id,
        detail={"text": text[:80]},
        ip=req.client.host if req.client else "",
    ))
    return JSONResponse({"ok": True, "annotation": annotation})


@router.delete("/api/tracks/{track_id}/annotations/{ann_id}")
async def api_annotations_delete(
    track_id: str, ann_id: str, current_user=Depends(require_operator()),
):
    """Delete an annotation (author or admin only)."""
    uname = getattr(current_user, "username", "anonymous")
    role  = getattr(current_user, "role", "")
    async with STATE_LOCK:
        anns = STATE["annotations"].get(track_id, [])
        target = next((a for a in anns if a["id"] == ann_id), None)
        if not target:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        # Only author or admin can delete
        is_admin = str(role).upper() == "ADMIN"
        if not is_admin and target["author"] != uname:
            return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
        STATE["annotations"][track_id] = [a for a in anns if a["id"] != ann_id]

    ev = {"event_type": "cop.annotation_removed",
          "payload": {"track_id": track_id, "id": ann_id}}
    _append_event_tail(ev)
    await broadcast(ev)
    return JSONResponse({"ok": True})
