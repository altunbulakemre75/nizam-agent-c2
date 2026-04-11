"""
cop/routers/tasks.py  —  Task approve/reject + fire-control pipeline

Extracted from cop/server.py. Contains:
  POST /api/tasks/{task_id}/approve
  POST /api/tasks/{task_id}/reject
  Fire-control helpers: effector impact, jam, spoof, EW-suppress
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from cop.state import (
    STATE, STATE_LOCK, CLIENTS_LOCK, TRACK_CLAIMS,
    AI_ASSIGNMENT, AI_ML_PREDICTIONS, AI_PREDICTIONS,
    AI_TRAJECTORIES, EFFECTOR_OUTCOMES, _track_histories,
)
from cop.ws_broadcast import broadcast, append_event_tail as _append_event_tail
from cop.helpers import utc_now_iso as _utc_now_iso
from cop.db_writes import (
    db_write as _db_write,
    persist_task_update as _persist_task_update,
)

from ai import lineage as ai_lineage
from ai import bda as ai_bda
from ai import escalation as ai_escalation
from ai import trajectory as ai_trajectory
from ai import drift as ai_drift
from ai import retrainer as ai_retrainer
from ai import track_fsm

try:
    from auth.deps import require_operator
    from cop import audit as cop_audit
except ImportError:
    def require_operator(): return lambda: None  # type: ignore
    cop_audit = None  # type: ignore

log = logging.getLogger("nizam.cop")
router = APIRouter()

# ── Fire control constants ────────────────────────────────────────────────────
_EFFECTOR_IMPACT_DELAY_S = 2.0   # weapon flight time simulation
_NL_EFFECT_DURATION_S    = 10.0  # jamming/spoofing duration
_EFFECTOR_COOLDOWN_S     = 5.0   # effector cooldown after ENGAGE


# ── Helpers ───────────────────────────────────────────────────────────────────

def _record_outcome(task_id: str, track_id: str, action: str, outcome: str) -> None:
    EFFECTOR_OUTCOMES.append({
        "task_id":   task_id,
        "track_id":  track_id,
        "action":    action,
        "outcome":   outcome,
        "timestamp": _utc_now_iso(),
    })
    if len(EFFECTOR_OUTCOMES) > 50:
        EFFECTOR_OUTCOMES[:] = EFFECTOR_OUTCOMES[-50:]


# ── Fire control tasks ────────────────────────────────────────────────────────

async def _effector_cooldown_reset(effector_id: str) -> None:
    await asyncio.sleep(_EFFECTOR_COOLDOWN_S)
    async with STATE_LOCK:
        st = STATE["effector_status"].get(effector_id, {})
        if st.get("status") == "COOLDOWN":
            st["status"]     = "READY"
            st["updated_at"] = _utc_now_iso()
    await broadcast({
        "event_type": "cop.effector_status",
        "payload": {
            "effector_id": effector_id,
            "status":      "READY",
            "server_time": _utc_now_iso(),
        },
    })


async def _run_effector_impact(target_id: str, task_id: str) -> None:
    track_fsm.on_engage(target_id)
    async with STATE_LOCK:
        target = STATE["tracks"].get(target_id)
        if not target:
            return
        target["track_state"] = "ENGAGING"
        lat = target.get("lat")
        lon = target.get("lon")

    await broadcast({
        "event_type": "cop.effector_impact",
        "payload": {
            "target_id":   target_id,
            "task_id":     task_id,
            "lat":         lat,
            "lon":         lon,
            "delay_s":     _EFFECTOR_IMPACT_DELAY_S,
            "server_time": _utc_now_iso(),
        },
    })

    try:
        ai_lineage.record(
            track_id=target_id,
            stage="fire_control",
            summary=f"ENGAGE approved → effector launched (flight time {_EFFECTOR_IMPACT_DELAY_S}s)",
            inputs={"task_id": task_id, "lat": lat, "lon": lon},
            outputs={"action": "effector_impact", "delay_s": _EFFECTOR_IMPACT_DELAY_S},
            rule="fire_control.engage",
        )
    except Exception:
        pass

    engaged_at = _utc_now_iso()
    async with STATE_LOCK:
        task_snap = STATE["tasks"].get(task_id, {})
    hit = ai_bda.roll_outcome(
        task_id=task_id, track_id=target_id,
        action="ENGAGE", operator=task_snap.get("resolved_by", ""),
        engaged_at=engaged_at,
    )

    await asyncio.sleep(_EFFECTOR_IMPACT_DELAY_S)
    outcome_label = "hit" if hit else "miss"

    if hit:
        track_fsm.on_destroyed(target_id)
        async with STATE_LOCK:
            STATE["tracks"].pop(target_id, None)
            STATE["threats"].pop(target_id, None)
            AI_PREDICTIONS.pop(target_id, None)
            AI_TRAJECTORIES.pop(target_id, None)
            ai_trajectory.drop_track(target_id)
            AI_ML_PREDICTIONS.pop(target_id, None)
            _track_histories.pop(target_id, None)
            ai_escalation.resolve(target_id)
        await broadcast({
            "event_type": "cop.track_removed",
            "payload": {"id": target_id, "reason": "engaged",
                        "task_id": task_id, "server_time": _utc_now_iso()},
        })
    else:
        track_fsm.on_engage(target_id)

    async with STATE_LOCK:
        EFFECTOR_OUTCOMES.append({
            "task_id":   task_id, "track_id":  target_id,
            "action":    "ENGAGE", "outcome":   outcome_label,
            "timestamp": _utc_now_iso(),
        })
        if len(EFFECTOR_OUTCOMES) > 50:
            EFFECTOR_OUTCOMES[:] = EFFECTOR_OUTCOMES[-50:]
        for a in AI_ASSIGNMENT.get("assignments", []):
            if a.get("threat_id") == target_id:
                eid = a["effector_id"]
                STATE["effector_status"][eid] = {
                    "status": "COOLDOWN", "updated_at": _utc_now_iso(), "task_id": task_id,
                }
                asyncio.create_task(_effector_cooldown_reset(eid))
                break

    await broadcast({
        "event_type": "cop.effector_outcome",
        "payload": {
            "task_id":     task_id, "track_id":    target_id,
            "action":      "ENGAGE", "outcome":     outcome_label,
            "bda_pending": not hit,  "server_time": _utc_now_iso(),
        },
    })


async def _run_jam_effect(target_id: str, task_id: str) -> None:
    async with STATE_LOCK:
        target = STATE["tracks"].get(target_id)
        if not target:
            return
        target["track_state"] = "JAMMED"
        lat, lon = target.get("lat"), target.get("lon")
    await broadcast({"event_type": "cop.jam_active", "payload": {
        "target_id": target_id, "task_id": task_id,
        "lat": lat, "lon": lon,
        "duration_s": _NL_EFFECT_DURATION_S, "server_time": _utc_now_iso(),
    }})
    async with STATE_LOCK:
        _record_outcome(task_id, target_id, "JAM", "suppressed")
    await broadcast({"event_type": "cop.effector_outcome", "payload": {
        "task_id": task_id, "track_id": target_id,
        "action": "JAM", "outcome": "suppressed", "server_time": _utc_now_iso(),
    }})


async def _run_spoof_effect(target_id: str, task_id: str) -> None:
    async with STATE_LOCK:
        target = STATE["tracks"].get(target_id)
        if not target:
            return
        target["track_state"] = "SPOOFED"
        lat, lon = target.get("lat"), target.get("lon")
    await broadcast({"event_type": "cop.spoof_active", "payload": {
        "target_id": target_id, "task_id": task_id,
        "lat": lat, "lon": lon,
        "duration_s": _NL_EFFECT_DURATION_S, "server_time": _utc_now_iso(),
    }})
    async with STATE_LOCK:
        _record_outcome(task_id, target_id, "SPOOF", "suppressed")
    await broadcast({"event_type": "cop.effector_outcome", "payload": {
        "task_id": task_id, "track_id": target_id,
        "action": "SPOOF", "outcome": "suppressed", "server_time": _utc_now_iso(),
    }})


async def _run_ew_suppress_effect(target_id: str, task_id: str) -> None:
    async with STATE_LOCK:
        target = STATE["tracks"].get(target_id)
        if not target:
            return
        target["track_state"] = "EW_SUPPRESSED"
        lat, lon = target.get("lat"), target.get("lon")
    await broadcast({"event_type": "cop.ew_suppress_active", "payload": {
        "target_id": target_id, "task_id": task_id,
        "lat": lat, "lon": lon,
        "duration_s": _NL_EFFECT_DURATION_S, "server_time": _utc_now_iso(),
    }})
    async with STATE_LOCK:
        _record_outcome(task_id, target_id, "EW_SUPPRESS", "suppressed")
    await broadcast({"event_type": "cop.effector_outcome", "payload": {
        "task_id": task_id, "track_id": target_id,
        "action": "EW_SUPPRESS", "outcome": "suppressed", "server_time": _utc_now_iso(),
    }})


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/api/tasks/{task_id}/approve")
async def api_task_approve(task_id: str, req: Request, current_user=Depends(require_operator())):
    body = await req.json() if req.headers.get("content-length", "0") != "0" else {}
    operator_id = getattr(current_user, "username", None) or body.get("operator", body.get("operator_id", "operator"))
    async with STATE_LOCK:
        task = STATE["tasks"].get(task_id)
        if not task:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        track_id_for_claim = task.get("track_id")
        async with CLIENTS_LOCK:
            owner = TRACK_CLAIMS.get(track_id_for_claim) if track_id_for_claim else None
        if owner and owner != operator_id:
            return JSONResponse(
                {"ok": False, "error": f"Track claimed by {owner} — cannot approve"},
                status_code=409,
            )
        task["status"]      = "APPROVED"
        task["resolved_at"] = _utc_now_iso()
        task["resolved_by"] = operator_id
        action    = task.get("action")
        target_id = task.get("track_id")

    ev = {"event_type": "cop.task_update", "payload": dict(task)}
    _append_event_tail(ev)
    await broadcast(ev)
    asyncio.create_task(_db_write(_persist_task_update(task)))

    if action == "ENGAGE" and target_id:
        ai_drift.record_feedback(target_id, "true_positive")
        ai_retrainer.record(target_id, AI_ML_PREDICTIONS.get(str(target_id)), "true_positive")

    if action in ("ENGAGE", "JAM", "SPOOF", "EW_SUPPRESS") and target_id:
        for a in AI_ASSIGNMENT.get("assignments", []):
            if a.get("threat_id") == target_id:
                eid = a["effector_id"]
                STATE["effector_status"][eid] = {
                    "status": "ENGAGED", "updated_at": _utc_now_iso(), "task_id": task["id"],
                }
                break

    if action == "ENGAGE" and target_id:
        asyncio.create_task(_run_effector_impact(str(target_id), task["id"]))
    elif action == "JAM" and target_id:
        asyncio.create_task(_run_jam_effect(str(target_id), task["id"]))
    elif action == "SPOOF" and target_id:
        asyncio.create_task(_run_spoof_effect(str(target_id), task["id"]))
    elif action == "EW_SUPPRESS" and target_id:
        asyncio.create_task(_run_ew_suppress_effect(str(target_id), task["id"]))

    if target_id:
        ai_escalation.acknowledge(str(target_id), operator_id)

    if cop_audit:
        asyncio.create_task(cop_audit.log_action(
            username=operator_id, role=getattr(current_user, "role", ""),
            action="APPROVE_TASK", resource_type="task", resource_id=task_id,
            detail={"task_action": action, "track_id": target_id},
            ip=req.client.host if req.client else "",
        ))
    return JSONResponse({"ok": True, "task": task})


@router.post("/api/tasks/{task_id}/reject")
async def api_task_reject(task_id: str, req: Request, current_user=Depends(require_operator())):
    body = await req.json() if req.headers.get("content-length", "0") != "0" else {}
    operator_id = getattr(current_user, "username", None) or body.get("operator", body.get("operator_id", "operator"))
    async with STATE_LOCK:
        task = STATE["tasks"].get(task_id)
        if not task:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        track_id_for_claim = task.get("track_id")
        async with CLIENTS_LOCK:
            owner = TRACK_CLAIMS.get(track_id_for_claim) if track_id_for_claim else None
        if owner and owner != operator_id:
            return JSONResponse(
                {"ok": False, "error": f"Track claimed by {owner} — cannot reject"},
                status_code=409,
            )
        task["status"]      = "REJECTED"
        task["resolved_at"] = _utc_now_iso()
        task["resolved_by"] = operator_id

    ev = {"event_type": "cop.task_update", "payload": dict(task)}
    _append_event_tail(ev)
    await broadcast(ev)
    asyncio.create_task(_db_write(_persist_task_update(task)))

    reject_track_id = task.get("track_id")
    if reject_track_id:
        ai_escalation.acknowledge(str(reject_track_id), operator_id)
        if task.get("action") == "ENGAGE":
            ai_drift.record_feedback(str(reject_track_id), "false_positive")
            ai_retrainer.record(
                str(reject_track_id),
                AI_ML_PREDICTIONS.get(str(reject_track_id)),
                "false_positive",
            )

    if cop_audit:
        asyncio.create_task(cop_audit.log_action(
            username=operator_id, role=getattr(current_user, "role", ""),
            action="REJECT_TASK", resource_type="task", resource_id=task_id,
            detail={"track_id": reject_track_id},
            ip=req.client.host if req.client else "",
        ))
    return JSONResponse({"ok": True, "task": task})
