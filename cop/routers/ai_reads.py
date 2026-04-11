"""cop/routers/ai_reads.py  —  Read-mostly AI endpoints.

Everything under /api/ai/ that just reads the AI_* rolling buffers
(populated by the tactical engine in cop/server.py) and returns them.
Also the non-mutating AI_LLM calls (briefing/chat/command/parse).

The mutating AI endpoints (/api/ai/ml/train, /api/ai/retrain, drift
feedback, kill-chain fabrication) live in cop/routers/ai_mutations.py
or stay in server.py until further extraction — they reach deeper into
the tactical engine plumbing.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from ai import aar as ai_aar
from ai import bda as ai_bda
from ai import escalation as ai_escalation
from ai import explain as ai_explain
from ai import lineage as ai_lineage
from ai import llm_advisor as ai_llm
from ai import ml_threat as ai_ml
from ai import timeline as ai_timeline
from ai import trajectory as ai_trajectory

from cop.state import (
    STATE,
    AI_PREDICTIONS,
    AI_TRAJECTORIES,
    AI_ANOMALIES,
    AI_RECOMMENDATIONS,
    AI_PRED_BREACHES,
    AI_UNCERTAINTY_CONES,
    AI_COORD_ATTACKS,
    AI_ROE_ADVISORIES,
    AI_ASSIGNMENT,
    AI_BFT_WARNINGS,
    AI_DRIFT_STATUS,
    AI_ML_PREDICTIONS,
)
from cop.helpers import utc_now_iso

router = APIRouter(tags=["ai"])


# ── Predictions / trajectories ──────────────────────────────────────────────
@router.get("/api/ai/predictions")
async def api_ai_predictions(track_id: Optional[str] = Query(None)):
    """Get Kalman predicted future positions for tracks."""
    if track_id:
        return JSONResponse({
            "track_id": track_id,
            "predictions": AI_PREDICTIONS.get(track_id, []),
        })
    return JSONResponse({"predictions": {k: v for k, v in AI_PREDICTIONS.items()}})


@router.get("/api/ai/trajectories")
async def api_ai_trajectories(track_id: Optional[str] = Query(None)):
    """Get LSTM trajectory predictions for tracks."""
    stats = ai_trajectory.stats()
    if track_id:
        return JSONResponse({
            "track_id":   track_id,
            "trajectory": AI_TRAJECTORIES.get(track_id, []),
            "model":      stats,
        })
    return JSONResponse({
        "count":       len(AI_TRAJECTORIES),
        "model":       stats,
        "trajectories": AI_TRAJECTORIES,
    })


# ── Anomalies / recommendations / breaches ─────────────────────────────────
@router.get("/api/ai/anomalies")
async def api_ai_anomalies(limit: int = Query(50, le=200)):
    """Get recent anomalies."""
    return JSONResponse({
        "count": len(AI_ANOMALIES),
        "anomalies": AI_ANOMALIES[-limit:],
    })


@router.get("/api/ai/recommendations")
async def api_ai_recommendations():
    """Get current tactical recommendations."""
    return JSONResponse({
        "count": len(AI_RECOMMENDATIONS),
        "recommendations": AI_RECOMMENDATIONS,
    })


@router.get("/api/ai/pred_breaches")
async def api_ai_pred_breaches():
    """Get predictive zone breach warnings."""
    return JSONResponse({
        "count": len(AI_PRED_BREACHES),
        "breaches": AI_PRED_BREACHES,
    })


@router.get("/api/ai/uncertainty")
async def api_ai_uncertainty(track_id: Optional[str] = Query(None)):
    """Get uncertainty cone data for predicted trajectories."""
    if track_id:
        return JSONResponse({
            "track_id": track_id,
            "cone": AI_UNCERTAINTY_CONES.get(track_id, []),
        })
    return JSONResponse({"cones": AI_UNCERTAINTY_CONES})


@router.get("/api/ai/coordinated")
async def api_ai_coordinated():
    """Get coordinated attack warnings."""
    return JSONResponse({
        "count": len(AI_COORD_ATTACKS),
        "attacks": AI_COORD_ATTACKS,
    })


# ── Timeline / lineage / explain ───────────────────────────────────────────
@router.get("/api/ai/timeline")
async def api_ai_timeline(track_id: Optional[str] = Query(None)):
    """Get threat timeline history for a track or all tracks."""
    if track_id:
        return JSONResponse({
            "track_id": track_id,
            "timeline": ai_timeline.get_timeline(track_id),
        })
    return JSONResponse({
        "tracks": ai_timeline.get_active_track_ids(),
        "timelines": ai_timeline.get_all_timelines(),
    })


@router.get("/api/ai/lineage/{track_id}")
async def api_ai_lineage(track_id: str):
    """Return the full decision lineage chain for a track."""
    chain = ai_lineage.get_chain(track_id)
    summary = ai_lineage.get_summary(track_id)
    return JSONResponse({"track_id": track_id, "summary": summary, "chain": chain})


@router.get("/api/ai/lineage")
async def api_ai_lineage_all():
    """Return lineage stats and all tracked IDs."""
    stats = ai_lineage.stats()
    track_ids = ai_lineage.get_all_track_ids()
    return JSONResponse({"stats": stats, "track_ids": track_ids})


@router.get("/api/ai/explain/{track_id}")
async def api_ai_explain(track_id: str):
    """Operator-facing explanation: why is this track the threat level it is?"""
    ml_pred = AI_ML_PREDICTIONS.get(str(track_id))
    track = STATE["tracks"].get(str(track_id))
    result = ai_explain.explain_track(track_id, ml_prediction=ml_pred, track=track)
    chain = ai_lineage.get_chain(track_id)
    result["lineage_tail"] = chain[-5:] if chain else []
    result["lineage_count"] = len(chain)
    return JSONResponse(result)


# ── ROE / escalation ───────────────────────────────────────────────────────
@router.get("/api/ai/roe")
async def api_ai_roe():
    """Get current ROE engagement advisories."""
    return JSONResponse({
        "count": len(AI_ROE_ADVISORIES),
        "advisories": AI_ROE_ADVISORIES,
        "escalation_pending": ai_escalation.get_pending(),
    })


# ── AAR ────────────────────────────────────────────────────────────────────
@router.get("/api/ai/aar")
async def api_ai_aar():
    """Generate and return After-Action Report."""
    report = ai_aar.generate_report(
        tracks=STATE["tracks"],
        threats=STATE["threats"],
        zones=STATE["zones"],
        assets=STATE["assets"],
        tasks=STATE["tasks"],
        timelines=ai_timeline.get_all_timelines(),
    )
    report["bda"] = {
        "summary": ai_bda.summary(),
        "records": ai_bda.get_all()[-20:],
        "pending": ai_bda.get_pending(),
    }
    return JSONResponse(report)


# ── ML model reads ─────────────────────────────────────────────────────────
@router.get("/api/ai/ml")
async def api_ai_ml(track_id: Optional[str] = Query(None)):
    """Get ML threat predictions."""
    if track_id:
        pred = AI_ML_PREDICTIONS.get(track_id)
        return JSONResponse({"track_id": track_id, "prediction": pred})
    return JSONResponse({
        "count": len(AI_ML_PREDICTIONS),
        "predictions": AI_ML_PREDICTIONS,
        "model": ai_ml.get_model_info(),
    })


@router.post("/api/ai/ml/train")
async def api_ai_ml_train():
    """Re-train ML model from recordings."""
    try:
        result = ai_ml.train()
        ai_ml.reload_from_disk()  # hot-swap — no cold-reload spike on next cycle
        return JSONResponse({"ok": True, "result": result})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ── Assignment / BFT / drift ───────────────────────────────────────────────
@router.get("/api/ai/assignment")
async def api_assignment():
    """Latest multi-effector assignment result from the Hungarian solver."""
    return JSONResponse({**AI_ASSIGNMENT, "server_time": utc_now_iso()})


@router.get("/api/ai/bft")
async def api_bft():
    """Latest blue-force fratricide warnings from the BFT checker."""
    return JSONResponse({
        "warnings":    AI_BFT_WARNINGS,
        "count":       len(AI_BFT_WARNINGS),
        "server_time": utc_now_iso(),
    })


@router.get("/api/ai/drift")
async def api_drift():
    """Current model drift status (PSI, grade dist, FPR)."""
    return JSONResponse({**AI_DRIFT_STATUS, "server_time": utc_now_iso()})


# ── LLM advisor (briefing / chat / command) ───────────────────────────────
@router.get("/api/ai/briefing")
async def api_ai_briefing():
    """Get AI-generated situation briefing."""
    result = await ai_llm.get_briefing(
        tracks=STATE["tracks"],
        threats=STATE["threats"],
        assets=STATE["assets"],
        zones=STATE["zones"],
        anomalies=AI_ANOMALIES,
        recommendations=AI_RECOMMENDATIONS,
    )
    return JSONResponse(result)


@router.post("/api/ai/chat")
async def api_ai_chat(req: Request):
    """Operator chat with AI advisor."""
    body = await req.json()
    question = body.get("question", "")
    if not question:
        return JSONResponse({"ok": False, "error": "question required"}, status_code=400)
    result = await ai_llm.chat(
        question=question,
        tracks=STATE["tracks"],
        threats=STATE["threats"],
        assets=STATE["assets"],
        zones=STATE["zones"],
        anomalies=AI_ANOMALIES,
        recommendations=AI_RECOMMENDATIONS,
        session_id=body.get("session_id", "default"),
    )
    return JSONResponse(result)


@router.post("/api/ai/command")
async def api_ai_command(req: Request):
    """Parse natural-language command via LLM."""
    body = await req.json()
    command = body.get("command", "")
    if not command:
        return JSONResponse({"ok": False, "error": "command required"}, status_code=400)
    result = await ai_llm.parse_command(
        command=command,
        tracks=STATE["tracks"],
        assets=STATE["assets"],
    )
    return JSONResponse(result)
