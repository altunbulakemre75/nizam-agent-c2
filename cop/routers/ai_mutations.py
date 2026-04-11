"""cop/routers/ai_mutations.py  —  Mutating AI endpoints.

ROE acknowledgement, kill-chain view, handover report, retrain triggers,
and /api/ai/status. These all touch STATE and the AI_* globals but only
in read-mostly ways (retrain trigger just spawns a background thread).

The actual tactical engine loop and ingest path still live in
cop/server.py and use these same globals directly.
"""
from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ai import aar as ai_aar
from ai import escalation as ai_escalation
from ai import llm_advisor as ai_llm
from ai import ml_threat as ai_ml
from ai import retrainer as ai_retrainer
from ai import timeline as ai_timeline
from ai import track_fsm
from ai import trajectory as ai_trajectory
from cop import audit as cop_audit
from cop.helpers import utc_now_iso
from cop.state import (
    STATE,
    AI_PREDICTIONS,
    AI_ANOMALIES,
    AI_RECOMMENDATIONS,
    AI_PRED_BREACHES,
    AI_COORD_ATTACKS,
    AI_ROE_ADVISORIES,
)
from replay import recorder as replay_recorder

try:
    from auth.deps import require_operator, require_viewer
except Exception:
    def require_operator():
        return lambda: None
    def require_viewer():
        return lambda: None

router = APIRouter(tags=["ai"])


# ── ROE operator acknowledgement ───────────────────────────────────────────
@router.post("/api/roe/{track_id}/ack")
async def api_roe_ack(track_id: str, req: Request,
                      current_user=Depends(require_operator())):
    """Operator explicitly acknowledges a ROE advisory escalation."""
    operator_id = getattr(current_user, "username", None) or "operator"
    acked = ai_escalation.acknowledge(track_id, operator_id)
    asyncio.create_task(cop_audit.log_action(
        username=operator_id,
        role=getattr(current_user, "role", ""),
        action="ROE_ACK", resource_type="track", resource_id=track_id,
        detail={"track_id": track_id},
        ip=req.client.host if req.client else "",
    ))
    return JSONResponse({"ok": True, "track_id": track_id, "acknowledged": acked})


# ── AI subsystem status ────────────────────────────────────────────────────
@router.get("/api/ai/status")
async def api_ai_status():
    """AI subsystem status (aggregate counts + provider flags)."""
    return JSONResponse({
        "predictions_active":    len(AI_PREDICTIONS),
        "anomalies_total":       len(AI_ANOMALIES),
        "recommendations_active": len(AI_RECOMMENDATIONS),
        "pred_breaches_active":  len(AI_PRED_BREACHES),
        "coord_attacks_active":  len(AI_COORD_ATTACKS),
        "roe_advisories_active": len(AI_ROE_ADVISORIES),
        "timeline":              ai_timeline.get_summary(),
        "aar":                   ai_aar.get_status(),
        "recording":             replay_recorder.get_status(),
        "ml_model":              ai_ml.get_model_info(),
        "llm_enabled":           ai_llm.LLM_ENABLED,
        "llm_provider":          ai_llm.LLM_PROVIDER if ai_llm.LLM_ENABLED else None,
        "track_fsm":             track_fsm.stats(),
        "lstm_trajectory":       ai_trajectory.stats(),
    })


# ── Kill chain view ────────────────────────────────────────────────────────
@router.get("/api/ai/kill_chain")
async def api_kill_chain():
    """Kill chain pipeline status for every active track.

    Stages, in order:
      DETECTED   — track seen by at least one sensor
      CLASSIFIED — threat assessment produced
      ROE        — ROE advisory issued
      TASKED     — at least one task created for the track
      ENGAGED    — an approved task exists
    """
    tracks  = STATE["tracks"]
    threats = STATE["threats"]

    roe_ids     = {a.get("track_id") for a in AI_ROE_ADVISORIES if a.get("track_id")}
    tasked_ids: set = set()
    engaged_ids: set = set()
    for task in STATE["tasks"].values():
        tid = task.get("track_id") or task.get("target_id")
        if tid:
            tasked_ids.add(tid)
            if task.get("status") == "APPROVED":
                engaged_ids.add(tid)

    pipeline = []
    for track_id, _track in tracks.items():
        threat = threats.get(track_id)
        level  = (threat or {}).get("threat_level", "UNKNOWN")
        intent = (threat or {}).get("intent", "unknown")
        engagement = next(
            (a.get("engagement") for a in AI_ROE_ADVISORIES if a.get("track_id") == track_id),
            None,
        )

        if track_id in engaged_ids:
            stage = "ENGAGED"
        elif track_id in tasked_ids:
            stage = "TASKED"
        elif track_id in roe_ids:
            stage = "ROE"
        elif threat is not None:
            stage = "CLASSIFIED"
        else:
            stage = "DETECTED"

        pipeline.append({
            "track_id":   track_id,
            "stage":      stage,
            "threat_level": level,
            "intent":     intent,
            "engagement": engagement,
        })

    stage_order = ["DETECTED", "CLASSIFIED", "ROE", "TASKED", "ENGAGED"]
    stage_counts = {s: 0 for s in stage_order}
    for p in pipeline:
        stage_counts[p["stage"]] = stage_counts.get(p["stage"], 0) + 1

    return JSONResponse({
        "pipeline":     pipeline,
        "stage_counts": stage_counts,
        "total":        len(pipeline),
        "server_time":  utc_now_iso(),
    })


# ── Retrainer triggers ─────────────────────────────────────────────────────
@router.post("/api/ai/retrain")
async def api_retrain(_=Depends(require_operator())):
    """Manually trigger model retraining using accumulated operator feedback."""
    result = ai_retrainer.trigger(blocking=False)
    return JSONResponse({**result, "server_time": utc_now_iso()})


@router.get("/api/ai/retrain/status")
async def api_retrain_status():
    """Current retraining status and feedback buffer stats."""
    return JSONResponse({**ai_retrainer.status(), "server_time": utc_now_iso()})


# ── Handover report ────────────────────────────────────────────────────────
@router.get("/api/handover")
async def api_handover(current_user=Depends(require_viewer())):
    """Shift handover report snapshot (active tracks, zones, alerts, tasks)."""
    operator = getattr(current_user, "username", "anonymous")

    track_rows = []
    for tid, tr in STATE["tracks"].items():
        threat = STATE["threats"].get(tid) or {}
        anns   = STATE["annotations"].get(tid, [])
        track_rows.append({
            "id":           tid,
            "lat":          (tr.get("kinematics") or {}).get("lat"),
            "lon":          (tr.get("kinematics") or {}).get("lon"),
            "threat_level": threat.get("threat_level") or tr.get("threat_level", "LOW"),
            "score":        threat.get("score"),
            "action":       threat.get("recommended_action"),
            "annotation_count": len(anns),
        })
    track_rows.sort(key=lambda r: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(r["threat_level"], 3))

    zone_rows = [
        {"id": z.get("id"), "name": z.get("name"), "type": z.get("type", "EXCLUSION")}
        for z in STATE["zones"].values()
    ]

    recent_alerts = [
        ev for ev in list(STATE["events_tail"])[-100:]
        if ev.get("event_type") in ("cop.alert", "cop.ew_alert", "cop.track_merged")
    ][-30:]

    pending_tasks = [
        {
            "id":          t.get("id"),
            "track_id":    t.get("track_id"),
            "action":      t.get("action"),
            "status":      t.get("status"),
            "proposed_by": t.get("proposed_by"),
        }
        for t in STATE["tasks"].values()
        if t.get("status") == "PENDING"
    ]

    return JSONResponse({
        "generated_at": utc_now_iso(),
        "generated_by": operator,
        "node_id":      os.environ.get("NODE_ID", "cop-node-01"),
        "tracks":       track_rows,
        "zones":        zone_rows,
        "recent_alerts": [ev.get("payload", {}) for ev in recent_alerts],
        "pending_tasks": pending_tasks,
        "summary": {
            "total_tracks": len(track_rows),
            "high_threats": sum(1 for r in track_rows if r["threat_level"] == "HIGH"),
            "medium_threats": sum(1 for r in track_rows if r["threat_level"] == "MEDIUM"),
            "total_zones":  len(zone_rows),
            "pending_tasks": len(pending_tasks),
            "annotated_tracks": sum(1 for r in track_rows if r["annotation_count"] > 0),
        },
    })
