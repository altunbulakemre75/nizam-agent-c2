"""cop/routers/reads.py  —  Trivial read endpoints for top-level state.

These used to live at the top of server.py — each is a one-liner that
just serialises a STATE bucket. Grouped here so the main file doesn't
need to declare them.
"""
from __future__ import annotations

import json
import os
import urllib.request

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from cop.helpers import utc_now_iso
from cop.state import STATE

router = APIRouter(tags=["reads"])

ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://127.0.0.1:8200")


@router.get("/api/agents")
async def api_agents():
    return JSONResponse({"agents": STATE["agents"], "server_time": utc_now_iso()})


@router.get("/api/orchestrator/health")
async def api_orchestrator_health():
    try:
        with urllib.request.urlopen(ORCHESTRATOR_URL + "/agents/health", timeout=2) as r:
            return JSONResponse(json.loads(r.read()))
    except Exception:
        return JSONResponse(
            {"ok": False, "agents": [], "total": 0, "alive": 0, "dead": 0},
            status_code=503,
        )


@router.get("/api/tracks")
async def api_tracks():
    return JSONResponse({"tracks": list(STATE["tracks"].values()), "server_time": utc_now_iso()})


@router.get("/api/threats")
async def api_threats():
    return JSONResponse({"threats": list(STATE["threats"].values()), "server_time": utc_now_iso()})


@router.get("/api/events_tail")
async def api_events_tail():
    return JSONResponse({"events_tail": STATE["events_tail"], "server_time": utc_now_iso()})


@router.get("/api/tasks")
async def api_tasks():
    return JSONResponse({"tasks": list(STATE["tasks"].values()), "server_time": utc_now_iso()})


@router.get("/api/effectors/telemetry")
async def api_effectors_telemetry():
    """Current effector operational status and recent engagement outcomes."""
    from cop.state import EFFECTOR_OUTCOMES
    return JSONResponse({
        "effector_status":  dict(STATE["effector_status"]),
        "recent_outcomes":  list(EFFECTOR_OUTCOMES[-20:]),
        "server_time":      utc_now_iso(),
    })
