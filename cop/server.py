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
from typing import Any, Dict, List

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("nizam.cop")

# ── AI Decision Support imports ──────────────────────────────────────────────
from ai import aar as ai_aar
from ai import ml_threat as ai_ml
from ai import bda as ai_bda
from cop.otel import init_tracing
from cop import sync as cop_sync
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
from cop.routers.tasks import router as tasks_router
from cop.routers.reset import router as reset_router
from cop.routers.ingest import router as ingest_router
from cop.routers.ws import router as ws_router
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
app.include_router(tasks_router)
app.include_router(reset_router)
app.include_router(ingest_router)
app.include_router(ws_router)

# ── CORS middleware ────────────────────────────────────────────────────────
# Whitelist of allowed origins, comma-separated. In dev, defaults to common
# localhost ports so the vanilla-JS frontend can hit /api/* and /ws cleanly.
# In prod, set ALLOWED_ORIGINS explicitly. Never use "*" — credentials need
# specific origins, and the wildcard combined with credentialed requests is
# silently blocked by browsers anyway.
from fastapi.middleware.cors import CORSMiddleware

_DEFAULT_DEV_ORIGINS = (
    "http://localhost:8100,"
    "http://127.0.0.1:8100,"
    "http://localhost:5173,"
    "http://127.0.0.1:5173"
)
_origins_csv = os.environ.get("ALLOWED_ORIGINS", _DEFAULT_DEV_ORIGINS)
_allowed_origins = [o.strip() for o in _origins_csv.split(",") if o.strip()]

if "*" in _allowed_origins:
    raise RuntimeError(
        "ALLOWED_ORIGINS contains '*'. Wildcard origin is unsafe with "
        "credentialed requests and silently blocked by browsers. "
        "Use an explicit comma-separated list instead."
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins     = _allowed_origins,
    allow_credentials = True,
    allow_methods     = ["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers     = ["Authorization", "Content-Type", "X-API-Key"],
)

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
from cop.state import METRICS, _TACTICAL_RECENT_MAX


# Rate limiter → cop/routers/ingest.py


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



# DB persistence → cop/db_writes.py
# persist_task / persist_task_update → cop/db_writes.py

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


# _check_zone_breaches → cop/routers/ingest.py


# _ACTION_MAP + _auto_task → cop/routers/ingest.py


# =============================================================================
# Routes
# =============================================================================

# Root pages ("/" and "/login") → cop/routers/root.py


# Simple read endpoints (agents, orchestrator health, tracks, threats,
# events_tail, tasks, effector telemetry) → cop/routers/reads.py


# Tasks (approve/reject) + fire-control → cop/routers/tasks.py





# Multi-operator listing + track claim + annotations → cop/routers/operators.py


# ── Waypoints ────────────────────────────────────────────────

# Waypoint endpoints → cop/routers/waypoints.py


# Reset → cop/routers/reset.py


# Ingest → cop/routers/ingest.py


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

from cop.engine.ai_pipeline import process_track as _ai_process_track
from cop.engine.ai_pipeline import schedule_ai_tactical as _schedule_ai_tactical

# Re-export engine internals so existing tests that do `srv._X` still work.
from cop.engine.ai_pipeline import (
    _ai_run_tactical_compute,
    _ai_tactical_background_task,
    _ai_tactical_bg_lock,
    _AI_TACTICAL_INTERVAL,
)
import cop.engine.ai_pipeline as _tac_engine  # noqa: E402
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


# WebSocket → cop/routers/ws.py
