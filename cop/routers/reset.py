"""
cop/routers/reset.py  —  POST /api/reset

Extracted from cop/server.py. Wipes all in-memory state and broadcasts
a clean snapshot so connected clients resynchronise immediately.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from cop.state import (
    STATE, STATE_LOCK,
    BREACH_STATE, TASK_EMITTED, TRACK_CLAIMS,
    AI_PREDICTIONS, AI_TRAJECTORIES, AI_ANOMALIES,
    AI_RECOMMENDATIONS, AI_PRED_BREACHES, AI_UNCERTAINTY_CONES,
    AI_COORD_ATTACKS, AI_ROE_ADVISORIES, AI_ASSIGNMENT,
    AI_BFT_WARNINGS, EFFECTOR_OUTCOMES, AI_DRIFT_STATUS,
    AI_ML_PREDICTIONS, AI_ML_PREV_TRACKS,
    _track_histories,
)
from cop.ws_broadcast import broadcast, append_event_tail as _append_event_tail
from cop.helpers import utc_now_iso as _utc_now_iso

from ai import trajectory as ai_trajectory
from ai import track_fsm
from ai import drift as ai_drift
from ai import retrainer as ai_retrainer
from ai import ml_threat as ai_ml
from ai import predictor as ai_predictor
from ai import anomaly as ai_anomaly
from ai import tactical as ai_tactical
from ai import llm_advisor as ai_llm
from ai import zone_breach as ai_zone_breach
from ai import coordinated_attack as ai_coord_attack
from ai import timeline as ai_timeline
from ai import aar as ai_aar
from ai import roe as ai_roe
from ai import lineage as ai_lineage
from ai import deconfliction as ai_deconfliction
from ai import ew_detector as ai_ew
from ai import ew_ml as ai_ew_ml
from ai import escalation as ai_escalation
from ai import bda as ai_bda

from cop import sync as cop_sync
from cop import circuit_breaker as cop_cb

try:
    from auth.deps import require_operator
    from cop import audit as cop_audit
except ImportError:
    def require_operator(): return lambda: None  # type: ignore
    cop_audit = None  # type: ignore

log = logging.getLogger("nizam.cop")
router = APIRouter()


@router.post("/api/reset")
async def api_reset(_=Depends(require_operator())):
    async with STATE_LOCK:
        STATE["tracks"].clear()
        STATE["threats"].clear()
        STATE["events_tail"].clear()
        STATE["tasks"].clear()
        BREACH_STATE.clear()
        TASK_EMITTED.clear()
        TRACK_CLAIMS.clear()
        AI_PREDICTIONS.clear()
        AI_TRAJECTORIES.clear()
        AI_ANOMALIES.clear()
        AI_RECOMMENDATIONS.clear()
        ai_trajectory.clear()
        track_fsm.clear()
        AI_PRED_BREACHES.clear()
        AI_UNCERTAINTY_CONES.clear()
        AI_COORD_ATTACKS.clear()
        AI_ROE_ADVISORIES.clear()
        AI_ASSIGNMENT.clear()
        AI_BFT_WARNINGS.clear()
        EFFECTOR_OUTCOMES.clear()
        STATE["effector_status"].clear()
        AI_DRIFT_STATUS.clear()
        ai_drift.reset()
        ai_retrainer.reset()
        AI_ML_PREDICTIONS.clear()
        AI_ML_PREV_TRACKS.clear()
        ai_ml.clear_feature_cache()
        if cop_audit:
            cop_audit.reset_chain_cache()
        ai_predictor.reset()
        ai_anomaly.reset()
        ai_tactical.reset()
        ai_llm.reset()
        ai_zone_breach.reset()
        ai_coord_attack.reset()
        ai_timeline.reset()
        ai_aar.reset()
        ai_roe.reset()
        ai_lineage.clear()
        ai_deconfliction.reset()
        ai_ew.reset()
        ai_ew_ml.reset()
        ai_escalation.reset()
        ai_bda.clear()
        _track_histories.clear()
        cop_sync.reset()
        cop_cb.reset()
        ai_aar.start_session()
        snapshot = {
            "event_type": "cop.snapshot",
            "payload": {
                "tracks": [], "threats": [], "reset": True,
                "server_time": _utc_now_iso(),
            },
        }
        _append_event_tail({"event_type": "cop.reset", "payload": {"server_time": _utc_now_iso()}})
    await broadcast(snapshot)
    return JSONResponse({"ok": True, "reset": True})
