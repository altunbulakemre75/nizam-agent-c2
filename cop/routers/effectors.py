"""cop/routers/effectors.py  —  Effector telemetry ingest + outcome reporting."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from cop.helpers import utc_now_iso
from cop.state import STATE, STATE_LOCK, EFFECTOR_OUTCOMES
from cop.ws_broadcast import broadcast

try:
    from auth.deps import require_operator
except Exception:
    def require_operator():
        return lambda: None

router = APIRouter(tags=["effectors"])


def _record_outcome(task_id: str, track_id: str, action: str, outcome: str) -> None:
    """Append an engagement outcome record (bounded ring buffer)."""
    EFFECTOR_OUTCOMES.append({
        "task_id":   task_id,
        "track_id":  track_id,
        "action":    action,
        "outcome":   outcome,
        "timestamp": utc_now_iso(),
    })
    if len(EFFECTOR_OUTCOMES) > 50:
        EFFECTOR_OUTCOMES[:] = EFFECTOR_OUTCOMES[-50:]


@router.post("/api/effectors/{effector_id}/telemetry")
async def api_effector_telemetry(
    effector_id: str, req: Request,
    _=Depends(require_operator()),
):
    """
    Report effector operational status and/or post-engagement outcome.

    Body fields:
      status   : "READY" | "ENGAGED" | "COOLDOWN" | "OFFLINE"  (required)
      outcome  : "hit" | "miss" | "partial"                     (optional)
      track_id : associated track                               (optional)
      action   : "ENGAGE" | "JAM" | "SPOOF" | "EW_SUPPRESS"    (optional)
      lat, lon : current effector position                      (optional)
    """
    body = await req.json()
    status = body.get("status", "READY")
    valid_statuses = {"READY", "ENGAGED", "COOLDOWN", "OFFLINE"}
    if status not in valid_statuses:
        return JSONResponse(
            {"ok": False, "error": f"status must be one of {sorted(valid_statuses)}"},
            status_code=400,
        )
    outcome  = body.get("outcome")
    track_id = body.get("track_id")

    async with STATE_LOCK:
        STATE["effector_status"][effector_id] = {
            "status":     status,
            "updated_at": utc_now_iso(),
            "task_id":    body.get("task_id"),
            "lat":        body.get("lat"),
            "lon":        body.get("lon"),
        }
        if outcome:
            valid_outcomes = {"hit", "miss", "partial"}
            if outcome not in valid_outcomes:
                return JSONResponse(
                    {"ok": False, "error": f"outcome must be one of {sorted(valid_outcomes)}"},
                    status_code=400,
                )
            _record_outcome(body.get("task_id") or "", track_id or "", body.get("action") or "", outcome)

    await broadcast({
        "event_type": "cop.effector_status",
        "payload": {
            "effector_id": effector_id,
            "status":      status,
            "server_time": utc_now_iso(),
        },
    })
    if outcome:
        await broadcast({
            "event_type": "cop.effector_outcome",
            "payload": {
                "effector_id": effector_id,
                "track_id":    track_id,
                "outcome":     outcome,
                "action":      body.get("action"),
                "server_time": utc_now_iso(),
            },
        })
    return JSONResponse({"ok": True, "effector_id": effector_id, "status": status})
