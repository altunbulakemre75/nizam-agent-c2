"""
cop/server.py  —  NIZAM COP  (Phase 5)
Phases 1-3: tracks, threats, zones, alerts, assets, tasks, waypoints
Phase 4   : PostgreSQL/TimescaleDB persistence + JWT auth (optional)
Phase 5   : AI Decision Support Layer
            - Kalman-filter track prediction
            - Anomaly & swarm detection
            - Tactical recommendation engine
            - LLM-powered operator advisor (Claude / OpenAI)

ENV:
  DATABASE_URL      postgresql+asyncpg://user:pass@host:5432/nizam
  AUTH_ENABLED      true | false (default false)
  JWT_SECRET        change in production
  ORCHESTRATOR_URL  http://127.0.0.1:8200
  ANTHROPIC_API_KEY sk-ant-...  (optional, for LLM advisor)
  OPENAI_API_KEY    sk-...      (optional, for LLM advisor)
  LLM_PROVIDER      anthropic | openai
"""
from __future__ import annotations

import asyncio
import logging
import os

# Load .env early so LLM_PROVIDER / OLLAMA_URL are available before ai modules import
try:
    from dotenv import load_dotenv as _load_dotenv
    from pathlib import Path as _Path
    _load_dotenv(_Path(__file__).parent.parent / ".env")
except ImportError:
    pass
import uuid as _uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from fastapi import Depends, FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("nizam.cop")

# ── AI Decision Support imports ──────────────────────────────────────────────
from ai import predictor as ai_predictor
from ai import anomaly as ai_anomaly
from ai import tactical as ai_tactical
from ai import llm_advisor as ai_llm
from ai import zone_breach as ai_zone_breach
from ai import coordinated_attack as ai_coord_attack
from ai import timeline as ai_timeline
from ai import aar as ai_aar
from ai import roe as ai_roe
from ai import ml_threat as ai_ml
from ai import confidence as ai_confidence
from ai import lineage as ai_lineage
from ai import trajectory as ai_trajectory
from ai import track_fsm
from ai import deconfliction as ai_deconfliction
from ai import ew_detector as ai_ew
from ai import ew_ml as ai_ew_ml
from ai import fusion as ai_fusion
from ai.fusion import SensorMeasurement as FusionMeasurement
from ai import escalation as ai_escalation
from ai import assignment as ai_assignment
from ai import blue_force as ai_blue_force
from ai import nonlethal as ai_nonlethal
from ai import drift as ai_drift
from ai import retrainer as ai_retrainer
from ai import bda as ai_bda
from ai import registry as ai_registry
from ai.registry import TacticalContext as _TacticalContext
from cop.otel import init_tracing, span as otel_span
from cop import sync as cop_sync
from cop import circuit_breaker as cop_cb
from replay import recorder as replay_recorder

# ── Optional DB / Auth imports ───────────────────────────────────────────────
try:
    from db.session import AsyncSessionLocal, engine
    from db.models import (
        AssetRecord, TaskRecord, WaypointRecord, ZoneRecord,
    )
    from db.init_db import init_db
    from auth.deps import AUTH_ENABLED, require_operator
    from auth.router import router as auth_router
    _DB_AVAILABLE = True
except ImportError:
    _DB_AVAILABLE = False
    AUTH_ENABLED  = False
    def require_operator(): return lambda: None

from cop import audit as cop_audit
from cop import webhooks as cop_webhooks
from cop.ratelimit import RateLimitMiddleware

DB_ENABLED = _DB_AVAILABLE and bool(os.environ.get("DATABASE_URL"))


# ── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 1) Suppress Windows WinError 10054 noise from ProactorEventLoop
    loop = asyncio.get_running_loop()
    def _exc_handler(loop, context):
        exc = context.get("exception")
        if isinstance(exc, (ConnectionResetError, OSError)):
            return
        loop.default_exception_handler(context)
    loop.set_exception_handler(_exc_handler)

    # 2) Database init + state restore
    if DB_ENABLED:
        try:
            await init_db(engine)
            await _restore_state_from_db()
            log.info("[cop] State restored from database.")
        except Exception as exc:
            log.warning("[cop] DB init failed — running in-memory only: %s", exc)

    # 3) Initialise OpenTelemetry tracing (no-op if OTEL_ENABLED != "true")
    if init_tracing(_app):
        log.info("[cop] OpenTelemetry tracing enabled → %s",
                 os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4317"))

    # 4) Pre-warm ML model so the first tactical cycle has zero cold-start disk I/O
    await asyncio.get_running_loop().run_in_executor(None, ai_ml.reload_from_disk)

    # 5) Start AAR session
    ai_aar.start_session()

    # 6) Start recording
    scenario_name = os.environ.get("NIZAM_SCENARIO", "live")
    rec_path = replay_recorder.start(scenario_name)
    log.info("[cop] Recording started: %s", rec_path)

    # 7) BDA monitor loop — checks pending miss outcomes every 10 s
    async def _bda_monitor_loop() -> None:
        while True:
            await asyncio.sleep(10)
            async with STATE_LOCK:
                alive = set(STATE["tracks"].keys())
            finalized = ai_bda.check_pending(alive)
            for rec in finalized:
                await broadcast({"event_type": "cop.bda", "payload": rec})
                log.info("[bda] %s → %s", rec["track_id"], rec["outcome"])
    _bda_task = asyncio.create_task(_bda_monitor_loop())

    # 8) Start peer sync push loop (no-op if no peers registered)
    _sync_task = cop_sync.start_push_loop(lambda: STATE)
    # Pre-register peers from env: COP_PEERS=http://node2:8100,http://node3:8100
    for peer_url in os.environ.get("COP_PEERS", "").split(","):
        peer_url = peer_url.strip()
        if peer_url:
            cop_sync.add_peer(peer_url)

    yield

    _bda_task.cancel()
    _sync_task.cancel()

    # Stop recording on shutdown
    summary = replay_recorder.stop()
    if summary:
        log.info("[cop] Recording saved: %s (%d frames, %.1fs)",
                 summary["path"], summary["frames"], summary["duration_s"])


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="NIZAM COP",
    version="1.1.0",
    description=(
        "Real-time Common Operating Picture (COP) with AI decision support.\n\n"
        "**Core:** Track ingestion, threat assessment, zone management, asset tracking\n\n"
        "**AI:** Kalman prediction, anomaly/swarm detection, EW attack detection, "
        "LLM advisor (Claude/OpenAI/Ollama), coordinated attack analysis, ROE engine\n\n"
        "**WebSocket:** `ws://<host>/ws` for real-time track/threat/alert streaming"
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
    openapi_tags=[
        {"name": "tracks",  "description": "Track CRUD and ingestion"},
        {"name": "threats", "description": "Threat assessments"},
        {"name": "zones",   "description": "Zone management (keep-out, engagement, safe)"},
        {"name": "assets",  "description": "Friendly/hostile asset registry"},
        {"name": "tasks",   "description": "Operator task queue (approve/reject)"},
        {"name": "ai",      "description": "AI decision support — predictions, anomalies, LLM advisor"},
        {"name": "system",  "description": "Metrics, health, reset"},
    ],
)

if DB_ENABLED and _DB_AVAILABLE:
    app.include_router(auth_router)

# Extracted domain routers (cop/routers/)
from cop.routers.weather import router as weather_router
from cop.routers.bda import router as bda_router
from cop.routers.scenarios import router as scenarios_router
from cop.routers.audit import router as audit_router
from cop.routers.webhooks import router as webhooks_router
from cop.routers.replay import router as replay_router
from cop.routers.zones import router as zones_router
from cop.routers.assets import router as assets_router
from cop.routers.waypoints import router as waypoints_router
from cop.routers.analytics import router as analytics_router
from cop.routers.fusion import router as fusion_router
from cop.routers.ai_reads import router as ai_reads_router
from cop.routers.reads import router as reads_router
from cop.routers.operators import router as operators_router
from cop.routers.sync import router as sync_router
from cop.routers.root import router as root_router
from cop.routers.metrics import router as metrics_router
from cop.routers.effectors import router as effectors_router
from cop.routers.ai_mutations import router as ai_mutations_router
app.include_router(weather_router)
app.include_router(bda_router)
app.include_router(scenarios_router)
app.include_router(audit_router)
app.include_router(webhooks_router)
app.include_router(replay_router)
app.include_router(zones_router)
app.include_router(assets_router)
app.include_router(waypoints_router)
app.include_router(analytics_router)
app.include_router(fusion_router)
app.include_router(ai_reads_router)
app.include_router(reads_router)
app.include_router(operators_router)
app.include_router(sync_router)
app.include_router(root_router)
app.include_router(metrics_router)
app.include_router(effectors_router)
app.include_router(ai_mutations_router)

# Rate limiting middleware (write endpoints only)
app.add_middleware(RateLimitMiddleware)

app.mount("/static", StaticFiles(directory="cop/static"), name="static")


# ── In-memory state ───────────────────────────────────────────────────────────

# ── Application state — lives in cop/state.py so routers can share it ──────
from cop.state import (
    STATE,
    BREACH_STATE,
    TASK_EMITTED,
    EVENT_TAIL_MAX,
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
    EFFECTOR_OUTCOMES,
    AI_DRIFT_STATUS,
    AI_ML_PREDICTIONS,
    AI_ML_PREV_TRACKS,
    AI_PLUGIN_RESULTS,
    AI_ANOMALY_MAX,
    CLIENTS,
    CLIENTS_LOCK,
    STATE_LOCK,
    OPERATORS,
    TRACK_CLAIMS,
    WS_OPERATORS,
    _TRACK_HISTORY_MAX,
    _track_histories,
)


# ── Metrics — lives in cop/state.py so routers can read counters ──────────
import time as _time_mod
from cop.state import METRICS, _METRICS_START_TS, _TACTICAL_RECENT_MAX


# ── Rate limiter (token bucket per IP) ──────────────────────────────────────
_RATE_LIMIT_RPS    = 200        # max requests per second per IP
_RATE_LIMIT_BURST  = 500        # burst capacity
_rate_buckets: Dict[str, list]  = {}    # {ip: [tokens, last_refill_time]}

def _rate_limit_check(ip: str) -> bool:
    """Return True if request is allowed, False if rate-limited."""
    now = _time_mod.monotonic()
    bucket = _rate_buckets.get(ip)
    if bucket is None:
        _rate_buckets[ip] = [_RATE_LIMIT_BURST - 1, now]
        return True
    tokens, last = bucket
    # Refill tokens
    elapsed = now - last
    tokens = min(_RATE_LIMIT_BURST, tokens + elapsed * _RATE_LIMIT_RPS)
    if tokens < 1.0:
        bucket[0] = tokens
        bucket[1] = now
        return False
    bucket[0] = tokens - 1
    bucket[1] = now
    return True


def _metrics_record_tactical_duration(ms: float) -> None:
    """Push a tactical run duration into the rolling window and update max."""
    METRICS["tactical_last_ms"] = round(ms, 2)
    if ms > METRICS["tactical_max_ms"]:
        METRICS["tactical_max_ms"] = round(ms, 2)
    recent: List[float] = METRICS["tactical_recent_ms"]
    recent.append(round(ms, 2))
    if len(recent) > _TACTICAL_RECENT_MAX:
        del recent[: len(recent) - _TACTICAL_RECENT_MAX]


def _metrics_percentile(values: List[float], pct: float) -> float:
    """Nearest-rank percentile. Returns 0.0 on empty input."""
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((pct / 100.0) * (len(s) - 1)))))
    return s[k]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str = "") -> str:
    return f"{prefix}{_uuid.uuid4().hex[:10]}"


def _append_event_tail(ev: Dict[str, Any]) -> None:
    tail: List[Dict[str, Any]] = STATE["events_tail"]
    tail.append(ev)
    if len(tail) > EVENT_TAIL_MAX:
        del tail[: len(tail) - EVENT_TAIL_MAX]


from cop.ws_broadcast import broadcast  # re-export for existing code in this file


def _point_in_polygon(lat: float, lon: float, coords: List) -> bool:
    n = len(coords)
    if n < 3:
        return False
    inside = False
    x, y   = lon, lat
    j      = n - 1
    for i in range(n):
        xi, yi = coords[i][1], coords[i][0]
        xj, yj = coords[j][1], coords[j][0]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-15) + xi):
            inside = not inside
        j = i
    return inside


# ── DB persistence (fire-and-forget) ─────────────────────────────────────────
# Extracted to cop/db_writes.py. Only the helpers still needed by server.py
# (ingest path + the not-yet-extracted task router) are re-imported here.
from cop.db_writes import (
    db_write       as _db_write,
    persist_track  as _persist_track,
    persist_threat as _persist_threat,
    persist_alert  as _persist_alert,
)


async def _persist_task(task: Dict[str, Any]) -> None:
    if not DB_ENABLED:
        return
    async with AsyncSessionLocal() as s:
        row = TaskRecord(
            id          =task["id"],
            track_id    =task["track_id"],
            action      =task["action"],
            threat_level=task.get("threat_level"),
            intent      =task.get("intent"),
            score       =task.get("score"),
            tti_s       =task.get("tti_s"),
            status      =task.get("status", "PENDING"),
        )
        await s.merge(row)
        await s.commit()


async def _persist_task_update(task: Dict[str, Any]) -> None:
    if not DB_ENABLED:
        return
    from sqlalchemy import update
    from datetime import datetime, timezone
    async with AsyncSessionLocal() as s:
        resolved_at = task.get("resolved_at")
        if resolved_at and isinstance(resolved_at, str):
            resolved_at = datetime.fromisoformat(resolved_at.replace("Z", "+00:00"))
        await s.execute(
            update(TaskRecord)
            .where(TaskRecord.id == task["id"])
            .values(
                status     =task["status"],
                resolved_at=resolved_at,
                resolved_by=task.get("resolved_by"),
            )
        )
        await s.commit()


# Waypoint DB writes → cop/db_writes.py


# ── State restore from DB ─────────────────────────────────────────────────────

async def _restore_state_from_db() -> None:
    """Load persistent state (zones, assets, tasks, waypoints) on startup."""
    if not DB_ENABLED:
        return
    from sqlalchemy import select
    async with AsyncSessionLocal() as s:
        for row in (await s.execute(select(ZoneRecord))).scalars():
            STATE["zones"][row.id] = {
                "id": row.id, "name": row.name, "type": row.type,
                "coordinates": row.coordinates, "color": row.color,
            }
        for row in (await s.execute(select(AssetRecord))).scalars():
            STATE["assets"][row.id] = {
                "id": row.id, "name": row.name, "type": row.type,
                "lat": row.lat, "lon": row.lon, "status": row.status,
            }
        for row in (await s.execute(select(TaskRecord))).scalars():
            STATE["tasks"][row.id] = {
                "id": row.id, "track_id": row.track_id, "action": row.action,
                "threat_level": row.threat_level, "intent": row.intent,
                "score": row.score, "tti_s": row.tti_s, "status": row.status,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
                "resolved_by": row.resolved_by,
            }
        for row in (await s.execute(select(WaypointRecord))).scalars():
            STATE["waypoints"][row.id] = {
                "id": row.id, "name": row.name, "lat": row.lat, "lon": row.lon,
                "order": row.order, "mission_id": row.mission_id,
            }


# ── Zone breach detection ─────────────────────────────────────────────────────

async def _check_zone_breaches(track_id: str, lat: float, lon: float) -> None:
    current_breaches: Set[str] = set()
    for zone_id, zone in STATE["zones"].items():
        coords = zone.get("coordinates", [])
        if _point_in_polygon(lat, lon, coords):
            current_breaches.add(zone_id)

    prev_breaches = BREACH_STATE.get(track_id, set())
    new_entries   = current_breaches - prev_breaches
    BREACH_STATE[track_id] = current_breaches

    for zone_id in new_entries:
        zone = STATE["zones"].get(zone_id, {})
        alert_payload = {
            "alert_type": "zone_breach",
            "track_id":   track_id,
            "zone_id":    zone_id,
            "zone_name":  zone.get("name", zone_id),
            "zone_type":  zone.get("type", "restricted"),
            "lat": lat, "lon": lon,
            "server_time": _utc_now_iso(),
        }
        alert = {"event_type": "cop.alert", "payload": alert_payload}
        _append_event_tail(alert)
        await broadcast(alert)
        asyncio.create_task(_db_write(_persist_alert(alert_payload)))
        ai_aar.record_zone_breach(alert_payload)
        asyncio.create_task(cop_webhooks.dispatch("cop.zone_breach", alert_payload))


# ── Autonomous tasking ────────────────────────────────────────────────────────

_ACTION_MAP = {
    "attack":         ("ENGAGE",  "HIGH"),
    "reconnaissance": ("OBSERVE", "MEDIUM"),
    "loitering":      ("OBSERVE", "MEDIUM"),
    "unknown":        ("OBSERVE", "HIGH"),
}


async def _auto_task(threat_id: str, threat_payload: Dict[str, Any]) -> None:
    level  = threat_payload.get("threat_level", "LOW")
    intent = threat_payload.get("intent", "unknown")

    if level not in ("HIGH", "MEDIUM"):
        return

    emitted = TASK_EMITTED.get(threat_id, set())
    action, _ = _ACTION_MAP.get(intent, ("OBSERVE", "HIGH"))

    if action == "ENGAGE" and level != "HIGH":
        action = "OBSERVE"

    task_key = f"{action}:{intent}"
    if task_key not in emitted:
        # Avoid duplicate PENDING tasks for the same action
        already = any(
            t["track_id"] == threat_id and t["action"] == action and t["status"] == "PENDING"
            for t in STATE["tasks"].values()
        )
        if not already:
            task = {
                "id":           _new_id("task-"),
                "track_id":     threat_id,
                "action":       action,
                "threat_level": level,
                "intent":       intent,
                "score":        threat_payload.get("score", 0),
                "tti_s":        threat_payload.get("tti_s"),
                "status":       "PENDING",
                "created_at":   _utc_now_iso(),
                "resolved_at":  None,
                "resolved_by":  None,
            }
            STATE["tasks"][task["id"]] = task
            TASK_EMITTED.setdefault(threat_id, set()).add(task_key)
            ev = {"event_type": "cop.task", "payload": task}
            _append_event_tail(ev)
            await broadcast(ev)
            asyncio.create_task(_db_write(_persist_task(task)))
            ai_aar.record_task(task)
            if level == "HIGH":
                asyncio.create_task(cop_webhooks.dispatch("cop.threat_high", {
                    "track_id": threat_id, "action": action,
                    "threat_level": level, "intent": intent,
                    "score": task["score"],
                }))
            try:
                ai_lineage.record(
                    track_id=threat_id,
                    stage="task_proposer",
                    summary=f"Auto-proposed {action} (intent={intent}, level={level})",
                    inputs={
                        "threat_level": level,
                        "intent": intent,
                        "score": threat_payload.get("score", 0),
                        "tti_s": threat_payload.get("tti_s"),
                    },
                    outputs={"task_id": task["id"], "action": action, "status": "PENDING"},
                    rule=f"auto_task.{intent}→{action}",
                )
            except Exception:
                pass

    # ── Non-lethal alternatives (alongside ENGAGE for score < 90) ─────────────
    if action == "ENGAGE":
        nl_options = ai_nonlethal.recommend(
            threat_id, threat_payload, dict(STATE["assets"])
        )
        for opt in nl_options:
            nl_action = opt["action"]
            nl_key    = f"{nl_action}:{intent}"
            if nl_key in TASK_EMITTED.get(threat_id, set()):
                continue
            already_nl = any(
                t["track_id"] == threat_id and t["action"] == nl_action and t["status"] == "PENDING"
                for t in STATE["tasks"].values()
            )
            if already_nl:
                continue
            nl_task = {
                "id":           _new_id("task-"),
                "track_id":     threat_id,
                "action":       nl_action,
                "threat_level": level,
                "intent":       intent,
                "score":        threat_payload.get("score", 0),
                "tti_s":        threat_payload.get("tti_s"),
                "effector_id":  opt.get("effector_id"),
                "effector_name": opt.get("effector_name"),
                "dist_km":      opt.get("dist_km"),
                "status":       "PENDING",
                "created_at":   _utc_now_iso(),
                "resolved_at":  None,
                "resolved_by":  None,
            }
            STATE["tasks"][nl_task["id"]] = nl_task
            TASK_EMITTED.setdefault(threat_id, set()).add(nl_key)
            nl_ev = {"event_type": "cop.task", "payload": nl_task}
            _append_event_tail(nl_ev)
            await broadcast(nl_ev)
            asyncio.create_task(_db_write(_persist_task(nl_task)))


def _make_snapshot_payload() -> Dict[str, Any]:
    return {
        "tracks":    list(STATE["tracks"].values()),
        "threats":   list(STATE["threats"].values()),
        "zones":     list(STATE["zones"].values()),
        "assets":    list(STATE["assets"].values()),
        "tasks":     [t for t in STATE["tasks"].values() if t["status"] == "PENDING"],
        "waypoints": list(STATE["waypoints"].values()),
        "predictions":       AI_PREDICTIONS,
        "trajectories":      AI_TRAJECTORIES,
        "anomalies":         AI_ANOMALIES[-20:],
        "recommendations":   AI_RECOMMENDATIONS,
        "pred_breaches":     AI_PRED_BREACHES,
        "uncertainty_cones": AI_UNCERTAINTY_CONES,
        "coord_attacks":     AI_COORD_ATTACKS,
        "roe_advisories":    AI_ROE_ADVISORIES,
        "ml_predictions":    AI_ML_PREDICTIONS,
        "server_time": _utc_now_iso(),
    }


# =============================================================================
# Routes
# =============================================================================

# Root pages ("/" and "/login") → cop/routers/root.py


# Simple read endpoints (agents, orchestrator health, tracks, threats,
# events_tail, tasks, effector telemetry) → cop/routers/reads.py


# ── Tasks (mutating) ──────────────────────────────────────────────────────

@app.post("/api/tasks/{task_id}/approve")
async def api_task_approve(task_id: str, req: Request, current_user=Depends(require_operator())):
    body = await req.json() if req.headers.get("content-length", "0") != "0" else {}
    operator_id = getattr(current_user, "username", None) or body.get("operator", body.get("operator_id", "operator"))
    async with STATE_LOCK:
        task = STATE["tasks"].get(task_id)
        if not task:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        # Claim check: if the track is claimed by another operator, deny.
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
        # Snapshot the action + target while still under the lock
        action    = task.get("action")
        target_id = task.get("track_id")
    ev = {"event_type": "cop.task_update", "payload": dict(task)}
    _append_event_tail(ev)
    await broadcast(ev)
    asyncio.create_task(_db_write(_persist_task_update(task)))

    # ── Drift + retrainer feedback: ENGAGE approved = true positive ──
    if action == "ENGAGE" and target_id:
        ai_drift.record_feedback(target_id, "true_positive")
        ai_retrainer.record(target_id, AI_ML_PREDICTIONS.get(str(target_id)), "true_positive")

    # ── Mark assigned effector as ENGAGED ──
    if action in ("ENGAGE", "JAM", "SPOOF", "EW_SUPPRESS") and target_id:
        for a in AI_ASSIGNMENT.get("assignments", []):
            if a.get("threat_id") == target_id:
                eid = a["effector_id"]
                STATE["effector_status"][eid] = {
                    "status":     "ENGAGED",
                    "updated_at": _utc_now_iso(),
                    "task_id":    task["id"],
                }
                break

    # ── Fire control loop ──
    # Dispatch the appropriate effect based on the approved task action.
    if action == "ENGAGE" and target_id:
        asyncio.create_task(_run_effector_impact(str(target_id), task["id"]))
    elif action == "JAM" and target_id:
        asyncio.create_task(_run_jam_effect(str(target_id), task["id"]))
    elif action == "SPOOF" and target_id:
        asyncio.create_task(_run_spoof_effect(str(target_id), task["id"]))
    elif action == "EW_SUPPRESS" and target_id:
        asyncio.create_task(_run_ew_suppress_effect(str(target_id), task["id"]))

    # Operator action counts as acknowledgement for escalation engine
    if target_id:
        ai_escalation.acknowledge(str(target_id), operator_id)

    asyncio.create_task(cop_audit.log_action(
        username=operator_id,
        role=getattr(current_user, "role", ""),
        action="APPROVE_TASK", resource_type="task", resource_id=task_id,
        detail={"task_action": action, "track_id": target_id},
        ip=req.client.host if req.client else "",
    ))
    return JSONResponse({"ok": True, "task": task})


# ── Fire control / effector pipeline ─────────────────────────

# How long after approval to wait before the impact lands (seconds).
# Represents weapon flight time; also gives the UI a beat to play its
# animation before the target vanishes.
_EFFECTOR_IMPACT_DELAY_S = 2.0

# Non-lethal effect duration (seconds) — how long jamming/spoofing persists
_NL_EFFECT_DURATION_S = 10.0

# Cooldown period after an ENGAGE before effector is READY again
_EFFECTOR_COOLDOWN_S = 5.0


async def _run_effector_impact(target_id: str, task_id: str) -> None:
    """
    Background task: resolve an ENGAGE order against a track.

    Sequence:
      1) FSM transition → ENGAGING.
      2) Broadcast cop.effector_impact{target, lat, lon, delay_s}.
         The UI uses this to start the expanding-circle animation.
      3) Sleep _EFFECTOR_IMPACT_DELAY_S to simulate weapon flight time.
      4) FSM transition → DESTROYED. Remove the track from STATE.
      5) Broadcast cop.track_removed so the UI drops the marker.

    If the target disappears mid-flight (e.g. fusion lost it), we still
    broadcast track_removed with whatever id we had so the UI cleans up
    the animation gracefully.
    """
    # 1) FSM → ENGAGING + look up the target's current position.
    track_fsm.on_engage(target_id)
    async with STATE_LOCK:
        target = STATE["tracks"].get(target_id)
        if not target:
            log.info("[fire] engage %s: target %s not found, aborting",
                     task_id, target_id)
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

    # Decision lineage: record the fire control decision.
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

    # 2) BDA — roll hit probability before weapon lands.
    engaged_at = _utc_now_iso()
    async with STATE_LOCK:
        task_snap = STATE["tasks"].get(task_id, {})
    hit = ai_bda.roll_outcome(
        task_id       = task_id,
        track_id      = target_id,
        action        = "ENGAGE",
        operator      = task_snap.get("resolved_by", ""),
        engaged_at    = engaged_at,
    )

    # 3) Simulate weapon flight time.
    await asyncio.sleep(_EFFECTOR_IMPACT_DELAY_S)

    outcome_label = "hit" if hit else "miss"

    if hit:
        # 4a) FSM → DESTROYED. Remove target from state.
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

        # Tell the UI to drop the marker.
        await broadcast({
            "event_type": "cop.track_removed",
            "payload": {
                "id":          target_id,
                "reason":      "engaged",
                "task_id":     task_id,
                "server_time": _utc_now_iso(),
            },
        })
        log.info("[fire] engage %s: target %s DESTROYED", task_id, target_id)
    else:
        # 4b) Miss — track survives; BDA monitor will confirm EVADED/DESTROYED_LATE.
        track_fsm.on_engage(target_id)   # revert ENGAGING → back to tracked
        log.info("[fire] engage %s: target %s MISS — monitoring for BDA", task_id, target_id)

    # Record outcome in legacy EFFECTOR_OUTCOMES list and update effector status.
    async with STATE_LOCK:
        EFFECTOR_OUTCOMES.append({
            "task_id":   task_id,
            "track_id":  target_id,
            "action":    "ENGAGE",
            "outcome":   outcome_label,
            "timestamp": _utc_now_iso(),
        })
        if len(EFFECTOR_OUTCOMES) > 50:
            EFFECTOR_OUTCOMES[:] = EFFECTOR_OUTCOMES[-50:]
        for a in AI_ASSIGNMENT.get("assignments", []):
            if a.get("threat_id") == target_id:
                eid = a["effector_id"]
                STATE["effector_status"][eid] = {
                    "status":     "COOLDOWN",
                    "updated_at": _utc_now_iso(),
                    "task_id":    task_id,
                }
                asyncio.create_task(_effector_cooldown_reset(eid))
                break

    await broadcast({
        "event_type": "cop.effector_outcome",
        "payload": {
            "task_id":     task_id,
            "track_id":    target_id,
            "action":      "ENGAGE",
            "outcome":     outcome_label,
            "bda_pending": not hit,
            "server_time": _utc_now_iso(),
        },
    })


async def _effector_cooldown_reset(effector_id: str) -> None:
    """After cooldown period, return effector to READY state."""
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


def _record_outcome(task_id: str, track_id: str, action: str, outcome: str) -> None:
    """Append an outcome record (called from NL fire control functions)."""
    EFFECTOR_OUTCOMES.append({
        "task_id":   task_id,
        "track_id":  track_id,
        "action":    action,
        "outcome":   outcome,
        "timestamp": _utc_now_iso(),
    })
    if len(EFFECTOR_OUTCOMES) > 50:
        EFFECTOR_OUTCOMES[:] = EFFECTOR_OUTCOMES[-50:]


async def _run_jam_effect(target_id: str, task_id: str) -> None:
    """Background task: apply RF jamming to a track (non-lethal)."""
    async with STATE_LOCK:
        target = STATE["tracks"].get(target_id)
        if not target:
            return
        target["track_state"] = "JAMMED"
        lat = target.get("lat")
        lon = target.get("lon")

    await broadcast({
        "event_type": "cop.jam_active",
        "payload": {
            "target_id":  target_id,
            "task_id":    task_id,
            "lat":        lat,
            "lon":        lon,
            "duration_s": _NL_EFFECT_DURATION_S,
            "server_time": _utc_now_iso(),
        },
    })
    async with STATE_LOCK:
        _record_outcome(task_id, target_id, "JAM", "suppressed")
    await broadcast({
        "event_type": "cop.effector_outcome",
        "payload": {
            "task_id":   task_id,
            "track_id":  target_id,
            "action":    "JAM",
            "outcome":   "suppressed",
            "server_time": _utc_now_iso(),
        },
    })
    log.info("[jam] task %s: target %s jammed for %.0fs", task_id, target_id, _NL_EFFECT_DURATION_S)


async def _run_spoof_effect(target_id: str, task_id: str) -> None:
    """Background task: apply GPS spoofing to a track (non-lethal)."""
    async with STATE_LOCK:
        target = STATE["tracks"].get(target_id)
        if not target:
            return
        target["track_state"] = "SPOOFED"
        lat = target.get("lat")
        lon = target.get("lon")

    await broadcast({
        "event_type": "cop.spoof_active",
        "payload": {
            "target_id":  target_id,
            "task_id":    task_id,
            "lat":        lat,
            "lon":        lon,
            "duration_s": _NL_EFFECT_DURATION_S,
            "server_time": _utc_now_iso(),
        },
    })
    async with STATE_LOCK:
        _record_outcome(task_id, target_id, "SPOOF", "suppressed")
    await broadcast({
        "event_type": "cop.effector_outcome",
        "payload": {
            "task_id":   task_id,
            "track_id":  target_id,
            "action":    "SPOOF",
            "outcome":   "suppressed",
            "server_time": _utc_now_iso(),
        },
    })
    log.info("[spoof] task %s: target %s spoofed for %.0fs", task_id, target_id, _NL_EFFECT_DURATION_S)


async def _run_ew_suppress_effect(target_id: str, task_id: str) -> None:
    """Background task: apply broadband EW suppression to a track (non-lethal)."""
    async with STATE_LOCK:
        target = STATE["tracks"].get(target_id)
        if not target:
            return
        target["track_state"] = "EW_SUPPRESSED"
        lat = target.get("lat")
        lon = target.get("lon")

    await broadcast({
        "event_type": "cop.ew_suppress_active",
        "payload": {
            "target_id":  target_id,
            "task_id":    task_id,
            "lat":        lat,
            "lon":        lon,
            "duration_s": _NL_EFFECT_DURATION_S,
            "server_time": _utc_now_iso(),
        },
    })
    async with STATE_LOCK:
        _record_outcome(task_id, target_id, "EW_SUPPRESS", "suppressed")
    await broadcast({
        "event_type": "cop.effector_outcome",
        "payload": {
            "task_id":   task_id,
            "track_id":  target_id,
            "action":    "EW_SUPPRESS",
            "outcome":   "suppressed",
            "server_time": _utc_now_iso(),
        },
    })
    log.info("[ew] task %s: target %s EW-suppressed for %.0fs", task_id, target_id, _NL_EFFECT_DURATION_S)


@app.post("/api/tasks/{task_id}/reject")
async def api_task_reject(task_id: str, req: Request, current_user=Depends(require_operator())):
    body = await req.json() if req.headers.get("content-length", "0") != "0" else {}
    operator_id = getattr(current_user, "username", None) or body.get("operator", body.get("operator_id", "operator"))
    async with STATE_LOCK:
        task = STATE["tasks"].get(task_id)
        if not task:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        # Claim check
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
        # Drift + retrainer feedback: ENGAGE rejected = false positive
        if task.get("action") == "ENGAGE":
            ai_drift.record_feedback(str(reject_track_id), "false_positive")
            ai_retrainer.record(
                str(reject_track_id),
                AI_ML_PREDICTIONS.get(str(reject_track_id)),
                "false_positive",
            )

    asyncio.create_task(cop_audit.log_action(
        username=operator_id,
        role=getattr(current_user, "role", ""),
        action="REJECT_TASK", resource_type="task", resource_id=task_id,
        detail={"track_id": reject_track_id},
        ip=req.client.host if req.client else "",
    ))
    return JSONResponse({"ok": True, "task": task})


# Multi-operator listing + track claim + annotations → cop/routers/operators.py


# ── Waypoints ────────────────────────────────────────────────

# Waypoint endpoints → cop/routers/waypoints.py


# ── Reset ────────────────────────────────────────────────────

@app.post("/api/reset")
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


# ── Ingest ────────────────────────────────────────────────────

INGEST_API_KEY = os.environ.get("INGEST_API_KEY", "")

@app.post("/ingest")
async def ingest(req: Request):
    client_ip = req.client.host if req.client else "unknown"

    # Circuit breaker — reject before any work if IP is in OPEN/HALF_OPEN probe
    cb_ok, cb_reason = cop_cb.check(client_ip)
    if not cb_ok:
        METRICS["ingest_bad_request"] += 1
        return JSONResponse({"ok": False, "error": cb_reason}, status_code=503,
                            headers={"Retry-After": "30"})

    # API key guard: when AUTH_ENABLED and INGEST_API_KEY is set,
    # require X-API-Key header for /ingest access.
    if AUTH_ENABLED and INGEST_API_KEY:
        provided = req.headers.get("x-api-key", "")
        if provided != INGEST_API_KEY:
            METRICS["ingest_bad_request"] += 1
            cop_cb.record_bad(client_ip)
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    # Rate limiting
    if not _rate_limit_check(client_ip):
        METRICS["ingest_bad_request"] += 1
        cop_cb.record_bad(client_ip)
        return JSONResponse({"ok": False, "error": "rate limited"}, status_code=429)

    # Size guard — reject payloads > 256 KB
    content_length = req.headers.get("content-length")
    if content_length and int(content_length) > 262_144:
        METRICS["ingest_bad_request"] += 1
        cop_cb.record_bad(client_ip)
        return JSONResponse({"ok": False, "error": "payload too large"}, status_code=413)

    try:
        body = await req.json()
    except Exception:
        METRICS["ingest_bad_request"] += 1
        cop_cb.record_bad(client_ip)
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    # Pydantic shape validation — rejects malformed envelopes cleanly instead
    # of letting them detonate inside the fusion engine. Uses the permissive
    # EventEnvelope (extra="allow") so downstream dict code keeps working.
    from pydantic import ValidationError
    from schemas.models import EventEnvelope
    try:
        envelope = EventEnvelope.model_validate(body)
    except ValidationError as exc:
        METRICS["ingest_bad_request"] += 1
        cop_cb.record_bad(client_ip)
        return JSONResponse(
            {"ok": False, "error": "schema validation failed", "detail": exc.errors()[:3]},
            status_code=400,
        )
    event_type = envelope.event_type
    payload    = envelope.payload

    # Validate event_type whitelist
    _VALID_EVENT_TYPES = {"cop.track", "cop.threat", "cop.zone", "cop.alert",
                          "cop.asset", "cop.task", "cop.waypoint"}
    if event_type not in _VALID_EVENT_TYPES:
        METRICS["ingest_bad_request"] += 1
        cop_cb.record_bad(client_ip)
        return JSONResponse({"ok": False, "error": f"unknown event_type: {event_type}"}, status_code=400)

    METRICS["ingest_total"] += 1
    METRICS["ingest_by_type"][event_type] = METRICS["ingest_by_type"].get(event_type, 0) + 1
    cop_cb.record_success(client_ip)


    if isinstance(payload, dict) and "server_time" not in payload:
        payload["server_time"] = _utc_now_iso()

    ev = {"event_type": event_type, "payload": payload}

    async with STATE_LOCK:
        _append_event_tail(ev)

        if event_type == "cop.track":
            track_id = (
                payload.get("id") or payload.get("track_id")
                or payload.get("global_track_id") or payload.get("gid")
            )
            if track_id is not None:
                raw_id = str(track_id)

                # ── Deconfliction: resolve alias or detect duplicate ──────────
                canonical_id = raw_id
                match = ai_deconfliction.find_match(payload, STATE["tracks"])
                if match is not None:
                    matched_id, score = match
                    if matched_id != raw_id:
                        # Merge: adopt canonical ID, fuse sensor lists
                        existing = STATE["tracks"].get(matched_id, {})
                        payload["supporting_sensors"] = ai_deconfliction.merge_sensors(
                            existing, payload
                        )
                        payload["id"] = matched_id
                        payload["_deconfliction"] = {
                            "alias": raw_id,
                            "canonical": matched_id,
                            "score": score,
                        }
                        ai_deconfliction.record_merge(raw_id, matched_id)
                        ai_aar.record_deconfliction_merge(raw_id, matched_id)
                        canonical_id = matched_id
                        # Broadcast merge event so UI can remove the duplicate marker
                        asyncio.create_task(broadcast({
                            "event_type": "cop.track_merged",
                            "payload": {
                                "alias_id":     raw_id,
                                "canonical_id": matched_id,
                                "score":        score,
                                "server_time":  _utc_now_iso(),
                            },
                        }))

                track_id = canonical_id

                # ── Multi-sensor fusion ──────────────────────────────────────
                # Feed this measurement into the fusion engine.  If multiple
                # sensors are reporting the same physical target, the engine
                # returns a covariance-weighted fused state.  We overwrite
                # lat/lon/speed/heading in the payload so the rest of the
                # pipeline (zone breach, EW, tactical, WS broadcast) always
                # sees the best-estimate position.
                sensors = payload.get("supporting_sensors", [])
                lat_raw = payload.get("lat")
                lon_raw = payload.get("lon")
                if lat_raw is not None and lon_raw is not None:
                    _sensor_id = sensors[0] if sensors else "unknown"
                    _meas = FusionMeasurement(
                        sensor_id   = _sensor_id,
                        track_hint  = str(track_id),
                        lat         = float(lat_raw),
                        lon         = float(lon_raw),
                        alt_m       = float((payload.get("kinematics") or {}).get("altitude_m") or
                                            payload.get("altitude_m") or 0.0),
                        speed_mps   = float((payload.get("kinematics") or {}).get("speed_mps") or
                                            payload.get("speed_mps") or 0.0),
                        heading_deg = float((payload.get("kinematics") or {}).get("heading_deg") or
                                            payload.get("heading_deg") or 0.0),
                        timestamp   = payload.get("server_time", _utc_now_iso()),
                    )
                    _fused = ai_fusion.engine.update(_meas)
                    # Overwrite payload position with fused best-estimate
                    payload["lat"] = _fused.lat
                    payload["lon"] = _fused.lon
                    payload["_fusion"] = {
                        "fused_id":             _fused.id,
                        "contributing_sensors": _fused.contributing_sensors,
                        "pos_std_m":            round(_fused.pos_std_m, 1),
                        "speed_std_mps":        round(_fused.speed_std_mps, 2),
                        "sensor_count":         len(_fused.contributing_sensors),
                    }
                    if payload.get("kinematics"):
                        payload["kinematics"]["altitude_m"]  = _fused.alt_m
                        payload["kinematics"]["speed_mps"]   = _fused.speed_mps
                        payload["kinematics"]["heading_deg"] = _fused.heading_deg

                # Track FSM: update lifecycle state
                fsm_state = track_fsm.on_update(str(track_id), sensors)
                payload["track_state"] = fsm_state.value

                STATE["tracks"][str(track_id)] = payload
                lat = payload.get("lat")
                lon = payload.get("lon")
                # ── Rolling breadcrumb trail ──────────────────────────────────
                if lat is not None and lon is not None:
                    _tid = str(track_id)
                    hist = _track_histories.setdefault(_tid, [])
                    hist.append({"lat": round(float(lat), 6), "lon": round(float(lon), 6)})
                    if len(hist) > _TRACK_HISTORY_MAX:
                        del hist[:len(hist) - _TRACK_HISTORY_MAX]
                    payload["history"] = list(hist)
                if lat is not None and lon is not None and STATE["zones"]:
                    await _check_zone_breaches(str(track_id), float(lat), float(lon))

                # ── EW detection: rule-based + statistical ML classifiers ──
                if lat is not None and lon is not None:
                    speed_ms  = float(payload.get("speed_ms") or payload.get("speed") or 0.0)
                    heading   = float(payload.get("heading_deg") or payload.get("heading") or 0.0)
                    ew_alerts = ai_ew.on_track_update(
                        str(track_id), float(lat), float(lon),
                        sensors=sensors,
                    )
                    ew_alerts += ai_ew_ml.on_track_update(
                        str(track_id), float(lat), float(lon),
                        speed_ms=speed_ms, heading=heading,
                    )
                    for alert in ew_alerts:
                        ai_aar.record_ew_alert(alert)
                        _ew_payload = {**alert, "server_time": _utc_now_iso()}
                        asyncio.create_task(broadcast({
                            "event_type": "cop.ew_alert",
                            "payload":    _ew_payload,
                        }))
                        asyncio.create_task(cop_webhooks.dispatch("cop.ew_alert", _ew_payload))

                # ── Phase 5: AI hooks on track update ──
                if lat is not None and lon is not None:
                    _ai_process_track(str(track_id), float(lat), float(lon),
                                      payload.get("intent", "unknown"))
                # AAR: record track
                ai_aar.record_track(str(track_id), len(STATE["tracks"]))

                # Decision lineage: first sighting of this track.
                try:
                    ai_lineage.record(
                        track_id=str(track_id),
                        stage="ingest",
                        summary=f"Track update — sensors: {', '.join(sensors) if sensors else 'fuser'}",
                        inputs={
                            "lat": lat, "lon": lon,
                            "speed": payload.get("speed"),
                            "heading": payload.get("heading"),
                            "classification": payload.get("classification"),
                            "sensors": sensors,
                        },
                        outputs={"state": fsm_state.value},
                        rule="cop.ingest",
                    )
                except Exception:
                    pass

            asyncio.create_task(_db_write(_persist_track(payload)))

        elif event_type == "cop.threat":
            threat_id = (
                payload.get("id") or payload.get("track_id")
                or payload.get("global_track_id") or payload.get("gid")
            )
            if threat_id is not None:
                STATE["threats"][str(threat_id)] = payload
                await _auto_task(str(threat_id), payload)
                # Timeline: record threat state
                ai_timeline.record_threat(
                    str(threat_id),
                    score=int(payload.get("score", payload.get("threat_score", 0))),
                    level=str(payload.get("threat_level", "LOW")),
                    intent=str(payload.get("intent", "unknown")),
                )
                # AAR: record threat
                ai_aar.record_threat(
                    str(threat_id),
                    score=int(payload.get("score", payload.get("threat_score", 0))),
                    level=str(payload.get("threat_level", "LOW")),
                    intent=str(payload.get("intent", "unknown")),
                )
                # Decision lineage: threat assessment received from fuser/agent.
                try:
                    t_level = str(payload.get("threat_level", "LOW"))
                    t_score = payload.get("score", payload.get("threat_score", 0))
                    t_intent = str(payload.get("intent", "unknown"))
                    ai_lineage.record(
                        track_id=str(threat_id),
                        stage="threat_assess",
                        summary=f"Threat → {t_level} (score={t_score}, intent={t_intent})",
                        inputs={
                            "threat_level": t_level,
                            "score": t_score,
                            "intent": t_intent,
                            "tti_s": payload.get("tti_s"),
                            "classification": payload.get("classification"),
                        },
                        outputs={"threat_level": t_level, "score": t_score},
                        rule="cop.threat_ingest",
                    )
                except Exception:
                    pass
            asyncio.create_task(_db_write(_persist_threat(payload)))

        elif event_type == "cop.snapshot":
            pass

    # ── Phase 5: fire-and-forget tactical analysis ──
    # Rate-limited internally. Runs in a thread pool executor so the heavy
    # compute (swarm/coord-attack/ML/ROE) does NOT block the event loop —
    # ingest returns immediately and the AI update is broadcast later when
    # the background task finishes. This is what keeps /ingest responsive
    # under 5+ drone scenarios.
    _schedule_ai_tactical()

    # ── Record frame for replay ──
    replay_recorder.capture_frame(_make_snapshot_payload)

    await broadcast(ev)

    return JSONResponse({"ok": True})


# ── Phase 5: Tactical engine → cop/engine/tactical.py ────────────────────────
#
# DESIGN NOTE (tactical offload):
#
# The tactical engine (swarm detect, recommendations, coord attack, ML,
# ROE) is pure CPU work and takes meaningful time under load (5+ tracks
# with ML enabled → hundreds of ms per tick). Originally it was called
# synchronously from /ingest inside the event loop. Rate-limited to 3s,
# but each invocation still froze the loop and caused /ingest timeouts
# during multi-drone scenarios (see multi_axis_attack: 239 POST timeouts).
#
# Fix: move the compute into a thread pool executor, driven by a fire-
# and-forget background task that /ingest merely schedules. /ingest now
# returns immediately after the fast state update + WS broadcast. The
# background task takes a brief state snapshot, runs the heavy compute
# off-loop, then applies results + broadcasts cop.ai_update.
#
# All engine functions live in cop/engine/tactical.py. server.py only
# keeps the two symbols it calls directly: _ai_process_track and
# _schedule_ai_tactical.

from cop.engine.tactical import process_track as _ai_process_track
from cop.engine.tactical import schedule_ai_tactical as _schedule_ai_tactical

# Re-export engine internals so existing tests that do `srv._X` still work.
from cop.engine.tactical import (
    _ai_run_tactical_compute,
    _ai_tactical_background_task,
    _ai_tactical_bg_lock,
    _AI_TACTICAL_INTERVAL,
)
import cop.engine.tactical as _tac_engine  # noqa: E402
# Mutable engine-state proxies: tests patch srv._ai_tactical_last directly.
# Redirect attribute access to the engine module so mutations take effect.
def __getattr__(name: str):
    if name == "_ai_tactical_last":
        return _tac_engine._ai_tactical_last
    raise AttributeError(name)

from cop.state import metrics_record_tactical_duration as _metrics_record_tactical_duration
from cop.state import _TACTICAL_RECENT_MAX


# ── Phase 5: AI API endpoints ───────────────────────────────

# AI read-only endpoints → cop/routers/ai_reads.py


# ROE ack + handover → cop/routers/ai_mutations.py


# Metrics endpoints (/api/metrics, /metrics) → cop/routers/metrics.py


# ── Distributed sync endpoints ────────────────────────────────────────────────

# Sync endpoints → cop/routers/sync.py


# /api/ai/status → cop/routers/ai_mutations.py


# Replay endpoints → cop/routers/replay.py


# Analytics endpoints → cop/routers/analytics.py


# Scenario endpoints → cop/routers/scenarios.py


# Audit endpoints → cop/routers/audit.py


# Kill chain + retrain endpoints → cop/routers/ai_mutations.py


# Effector telemetry POST → cop/routers/effectors.py


# BDA endpoint → cop/routers/bda.py


# Effector telemetry GET → cop/routers/reads.py


# Asset PATCH → cop/routers/assets.py


# Duplicate analytics set was dead code (shadowed by the live routes already
# extracted to cop/routers/analytics.py) — removed during the server.py
# breakup. The cop.analytics helpers remain available if a future rewrite
# wants to build proper hours-binned summary endpoints.


# ── Webhooks ───────────────────────────────────────────────────────────────────

# Fusion endpoint → cop/routers/fusion.py


# Webhook endpoints → cop/routers/webhooks.py


# ── WebSocket ─────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(
    websocket:   WebSocket,
    token:       Optional[str] = Query(None),
    operator_id: Optional[str] = Query(None),
):
    # Auth check for WebSocket (via query param)
    if AUTH_ENABLED:
        from auth.deps import _decode_token
        username = _decode_token(token or "")
        if not username:
            await websocket.close(code=4001)
            return

    # Resolve operator identity
    op_id = (operator_id or "").strip() or f"OPS-{_new_id('')[0:6].upper()}"

    await websocket.accept()
    async with CLIENTS_LOCK:
        CLIENTS.add(websocket)
        OPERATORS[op_id] = {"joined_at": _utc_now_iso(), "op_id": op_id}
        WS_OPERATORS[id(websocket)] = op_id

    # Announce join to all other clients
    await broadcast({
        "event_type": "cop.operator_joined",
        "payload": {
            "operator_id": op_id,
            "operators":   [{"operator_id": k, "joined_at": v["joined_at"]}
                            for k, v in OPERATORS.items()],
            "claims":      dict(TRACK_CLAIMS),
            "server_time": _utc_now_iso(),
        },
    })

    try:
        # Send full state snapshot to this client
        async with STATE_LOCK:
            snapshot = {"event_type": "cop.snapshot", "payload": _make_snapshot_payload()}
        # Attach current operator state to snapshot
        snapshot["payload"]["operators"] = [
            {"operator_id": k, "joined_at": v["joined_at"]}
            for k, v in OPERATORS.items()
        ]
        snapshot["payload"]["claims"] = dict(TRACK_CLAIMS)
        await websocket.send_json(snapshot)

        # Heartbeat: send ping every 10s so dead connections surface quickly.
        while True:
            await asyncio.sleep(10)
            try:
                await websocket.send_json({"event_type": "cop.ping", "payload": {"t": _utc_now_iso()}})
            except Exception:
                break

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        async with CLIENTS_LOCK:
            CLIENTS.discard(websocket)
            OPERATORS.pop(op_id, None)
            WS_OPERATORS.pop(id(websocket), None)
            # Release all claims held by this operator
            released = [tid for tid, oid in list(TRACK_CLAIMS.items()) if oid == op_id]
            for tid in released:
                TRACK_CLAIMS.pop(tid, None)

        if released:
            for tid in released:
                await broadcast({
                    "event_type": "cop.track_released",
                    "payload": {"track_id": tid, "operator_id": op_id,
                                "reason": "disconnect", "server_time": _utc_now_iso()},
                })

        await broadcast({
            "event_type": "cop.operator_left",
            "payload": {
                "operator_id": op_id,
                "operators":   [{"operator_id": k, "joined_at": v["joined_at"]}
                                for k, v in OPERATORS.items()],
                "released_tracks": released,
                "server_time": _utc_now_iso(),
            },
        })
