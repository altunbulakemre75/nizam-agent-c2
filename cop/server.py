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
import json
import logging
import os
import urllib.request
from pathlib import Path

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
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://127.0.0.1:8200")

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
from ai import lineage as ai_lineage
from ai import trajectory as ai_trajectory
from ai import track_fsm
from ai import deconfliction as ai_deconfliction
from ai import ew_detector as ai_ew
from cop import sync as cop_sync
from replay import recorder as replay_recorder
from replay import player as replay_player

# ── Optional DB / Auth imports ───────────────────────────────────────────────
try:
    from db.session import AsyncSessionLocal, engine
    from db.models import (
        AlertRecord, AssetRecord, TaskRecord,
        TrackEvent, ThreatEvent, WaypointRecord, ZoneRecord,
    )
    from db.init_db import init_db
    from auth.deps import AUTH_ENABLED, require_operator
    from auth.router import router as auth_router
    _DB_AVAILABLE = True
except ImportError:
    _DB_AVAILABLE = False
    AUTH_ENABLED  = False
    def get_db():   yield None
    def get_current_user(): return None
    def require_operator(): return lambda: None

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

    # 3) Start AAR session
    ai_aar.start_session()

    # 4) Start recording
    scenario_name = os.environ.get("NIZAM_SCENARIO", "live")
    rec_path = replay_recorder.start(scenario_name)
    log.info("[cop] Recording started: %s", rec_path)

    # 5) Start peer sync push loop (no-op if no peers registered)
    _sync_task = cop_sync.start_push_loop(lambda: STATE)
    # Pre-register peers from env: COP_PEERS=http://node2:8100,http://node3:8100
    for peer_url in os.environ.get("COP_PEERS", "").split(","):
        peer_url = peer_url.strip()
        if peer_url:
            cop_sync.add_peer(peer_url)

    yield

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

templates = Jinja2Templates(directory="cop/templates")
app.mount("/static", StaticFiles(directory="cop/static"), name="static")


# ── In-memory state ───────────────────────────────────────────────────────────

STATE: Dict[str, Any] = {
    "agents":      {},
    "tracks":      {},
    "threats":     {},
    "zones":       {},
    "assets":      {},
    "tasks":       {},
    "waypoints":   {},
    "events_tail": [],
}

BREACH_STATE: Dict[str, Set[str]] = {}
TASK_EMITTED: Dict[str, Set[str]] = {}
EVENT_TAIL_MAX = 500

# Phase 5 — AI state
AI_PREDICTIONS: Dict[str, List[Dict]] = {}   # {track_id: [Kalman predicted points]}
AI_TRAJECTORIES: Dict[str, List[Dict]] = {}  # {track_id: [LSTM predicted waypoints]}
AI_ANOMALIES: List[Dict] = []                # recent anomalies (max 100)
AI_RECOMMENDATIONS: List[Dict] = []           # latest tactical recommendations
AI_PRED_BREACHES: List[Dict] = []             # predictive zone breach warnings
AI_UNCERTAINTY_CONES: Dict[str, List[Dict]] = {}  # uncertainty cones for frontend
AI_COORD_ATTACKS: List[Dict] = []                 # coordinated attack warnings
AI_ROE_ADVISORIES: List[Dict] = []                # ROE engagement advisories
AI_ML_PREDICTIONS: Dict[str, Dict] = {}               # ML threat predictions per track
AI_ML_PREV_TRACKS: Dict[str, Dict] = {}               # previous frame tracks for acceleration calc
AI_ANOMALY_MAX = 100

CLIENTS: Set[WebSocket] = set()
CLIENTS_LOCK = asyncio.Lock()
STATE_LOCK   = asyncio.Lock()

# ── Multi-operator state ──────────────────────────────────────────────────────
#
# OPERATORS  : {operator_id: {joined_at, ws_ref (weakref)}} — active sessions
# TRACK_CLAIMS: {track_id: operator_id} — claimed tracks (one owner at a time)
# WS_OPERATORS: {websocket: operator_id} — reverse map for disconnect cleanup
#
OPERATORS: Dict[str, Dict] = {}
TRACK_CLAIMS: Dict[str, str] = {}
WS_OPERATORS: Dict[int, str] = {}   # id(websocket) → operator_id


# ── Metrics ───────────────────────────────────────────────────────────────────
#
# Lightweight runtime counters / timings so the next performance problem can
# be diagnosed by looking at numbers instead of guessing. Kept in-process:
# this is a single-server system, no Prometheus dep.

import time as _time_mod

_METRICS_START_TS: float = _time_mod.time()

METRICS: Dict[str, Any] = {
    # Ingest counters (incremented inside /ingest)
    "ingest_total":         0,
    "ingest_by_type":       {},          # {"cop.track": N, "cop.threat": N, ...}
    "ingest_bad_request":   0,
    # Tactical engine counters
    "tactical_scheduled":   0,           # times _schedule_ai_tactical was asked
    "tactical_rate_skipped": 0,           # skipped because < _AI_TACTICAL_INTERVAL
    "tactical_ran":         0,           # background task actually ran
    "tactical_overlap_skipped": 0,       # background task skipped because prev still in flight
    "tactical_failed":      0,           # executor raised
    "tactical_last_ms":     0.0,         # duration of the most recent run
    "tactical_max_ms":      0.0,         # worst-case seen so far
    "tactical_recent_ms":   [],          # rolling window of last 32 run durations
    "tactical_module_ms":   {},          # per-module timing of last run
    # WebSocket fan-out
    "ws_clients":           0,           # current count
    "ws_broadcasts":        0,           # total broadcast calls
    "ws_messages_sent":     0,           # total individual ws send_json calls
    "ws_send_failures":     0,           # dead clients dropped
}

_TACTICAL_RECENT_MAX = 32


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


async def broadcast(ev: Dict[str, Any]) -> None:
    dead: List[WebSocket] = []
    sent = 0
    async with CLIENTS_LOCK:
        for ws in list(CLIENTS):
            try:
                await ws.send_json(ev)
                sent += 1
            except Exception:
                dead.append(ws)
        for ws in dead:
            CLIENTS.discard(ws)
    METRICS["ws_broadcasts"]    += 1
    METRICS["ws_messages_sent"] += sent
    if dead:
        METRICS["ws_send_failures"] += len(dead)
    METRICS["ws_clients"] = len(CLIENTS)


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

async def _db_write(coro) -> None:
    """Run a coroutine that writes to DB; log and swallow any error."""
    if not DB_ENABLED:
        coro.close()  # prevent 'coroutine never awaited' RuntimeWarning
        return
    try:
        await coro
    except Exception as exc:
        log.debug("[db] write error: %s", exc)


async def _persist_track(payload: Dict[str, Any]) -> None:
    if not DB_ENABLED:
        return
    async with AsyncSessionLocal() as s:
        tid = (payload.get("id") or payload.get("track_id")
               or payload.get("global_track_id") or payload.get("gid"))
        row = TrackEvent(
            track_id=str(tid) if tid else "unknown",
            lat     =payload.get("lat"),
            lon     =payload.get("lon"),
            altitude=payload.get("altitude") or payload.get("alt"),
            speed   =payload.get("speed"),
            heading =payload.get("heading"),
            source  =payload.get("source"),
            raw     =payload,
        )
        s.add(row)
        await s.commit()


async def _persist_threat(payload: Dict[str, Any]) -> None:
    if not DB_ENABLED:
        return
    async with AsyncSessionLocal() as s:
        tid = (payload.get("id") or payload.get("track_id")
               or payload.get("global_track_id") or payload.get("gid"))
        row = ThreatEvent(
            track_id    =str(tid) if tid else "unknown",
            threat_level=payload.get("threat_level"),
            intent      =payload.get("intent"),
            score       =payload.get("score"),
            tti_s       =payload.get("tti_s"),
            raw         =payload,
        )
        s.add(row)
        await s.commit()


async def _persist_alert(payload: Dict[str, Any]) -> None:
    if not DB_ENABLED:
        return
    async with AsyncSessionLocal() as s:
        row = AlertRecord(
            track_id =payload.get("track_id", ""),
            zone_id  =payload.get("zone_id"),
            zone_name=payload.get("zone_name"),
            zone_type=payload.get("zone_type"),
            lat      =payload.get("lat"),
            lon      =payload.get("lon"),
        )
        s.add(row)
        await s.commit()


async def _persist_zone(zone: Dict[str, Any]) -> None:
    if not DB_ENABLED:
        return
    async with AsyncSessionLocal() as s:
        row = ZoneRecord(
            id         =zone["id"],
            name       =zone["name"],
            type       =zone.get("type", "restricted"),
            coordinates=zone["coordinates"],
            color      =zone.get("color"),
        )
        await s.merge(row)
        await s.commit()


async def _delete_zone_db(zone_id: str) -> None:
    if not DB_ENABLED:
        return
    from sqlalchemy import delete
    async with AsyncSessionLocal() as s:
        await s.execute(delete(ZoneRecord).where(ZoneRecord.id == zone_id))
        await s.commit()


async def _persist_asset(asset: Dict[str, Any]) -> None:
    if not DB_ENABLED:
        return
    async with AsyncSessionLocal() as s:
        row = AssetRecord(
            id    =asset["id"],
            name  =asset["name"],
            type  =asset.get("type", "unknown"),
            lat   =asset["lat"],
            lon   =asset["lon"],
            status=asset.get("status", "active"),
        )
        await s.merge(row)
        await s.commit()


async def _delete_asset_db(asset_id: str) -> None:
    if not DB_ENABLED:
        return
    from sqlalchemy import delete
    async with AsyncSessionLocal() as s:
        await s.execute(delete(AssetRecord).where(AssetRecord.id == asset_id))
        await s.commit()


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


async def _persist_waypoint(wp: Dict[str, Any]) -> None:
    if not DB_ENABLED:
        return
    async with AsyncSessionLocal() as s:
        row = WaypointRecord(
            id        =wp["id"],
            name      =wp["name"],
            lat       =wp["lat"],
            lon       =wp["lon"],
            order     =wp.get("order", 0),
            mission_id=wp.get("mission_id", "default"),
        )
        await s.merge(row)
        await s.commit()


async def _delete_waypoint_db(wp_id: str) -> None:
    if not DB_ENABLED:
        return
    from sqlalchemy import delete
    async with AsyncSessionLocal() as s:
        await s.execute(delete(WaypointRecord).where(WaypointRecord.id == wp_id))
        await s.commit()


async def _clear_waypoints_db() -> None:
    if not DB_ENABLED:
        return
    from sqlalchemy import delete
    async with AsyncSessionLocal() as s:
        await s.execute(delete(WaypointRecord))
        await s.commit()


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
    if task_key in emitted:
        return

    for t in STATE["tasks"].values():
        if t["track_id"] == threat_id and t["action"] == action and t["status"] == "PENDING":
            return

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

    # Decision lineage: record the auto-task creation against the track.
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

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html")


@app.get("/api/agents")
async def api_agents():
    return JSONResponse({"agents": STATE["agents"], "server_time": _utc_now_iso()})


@app.get("/api/orchestrator/health")
async def api_orchestrator_health():
    try:
        with urllib.request.urlopen(ORCHESTRATOR_URL + "/agents/health", timeout=2) as r:
            import json
            return JSONResponse(json.loads(r.read()))
    except Exception:
        return JSONResponse({"ok": False, "agents": [], "total": 0, "alive": 0, "dead": 0}, status_code=503)


@app.get("/api/tracks")
async def api_tracks():
    return JSONResponse({"tracks": list(STATE["tracks"].values()), "server_time": _utc_now_iso()})


@app.get("/api/threats")
async def api_threats():
    return JSONResponse({"threats": list(STATE["threats"].values()), "server_time": _utc_now_iso()})


@app.get("/api/events_tail")
async def api_events_tail():
    return JSONResponse({"events_tail": STATE["events_tail"], "server_time": _utc_now_iso()})


# ── Zones ────────────────────────────────────────────────────

@app.get("/api/zones")
async def api_zones():
    return JSONResponse({"zones": list(STATE["zones"].values()), "server_time": _utc_now_iso()})


@app.post("/api/zones")
async def api_zones_create(req: Request, _=Depends(require_operator())):
    body = await req.json()
    zone_id = body.get("id")
    if not zone_id or not body.get("coordinates"):
        return JSONResponse({"ok": False, "error": "id and coordinates required"}, status_code=400)
    zone = {
        "id":          zone_id,
        "name":        body.get("name", zone_id),
        "type":        body.get("type", "restricted"),
        "coordinates": body["coordinates"],
        "color":       body.get("color"),
        "created_at":  _utc_now_iso(),
    }
    async with STATE_LOCK:
        STATE["zones"][zone_id] = zone
    ev = {"event_type": "cop.zone", "payload": zone}
    _append_event_tail(ev)
    await broadcast(ev)
    asyncio.create_task(_db_write(_persist_zone(zone)))
    return JSONResponse({"ok": True, "zone": zone})


@app.delete("/api/zones/{zone_id}")
async def api_zones_delete(zone_id: str, _=Depends(require_operator())):
    async with STATE_LOCK:
        removed = STATE["zones"].pop(zone_id, None)
    if not removed:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    ev = {"event_type": "cop.zone_removed", "payload": {"id": zone_id}}
    _append_event_tail(ev)
    await broadcast(ev)
    asyncio.create_task(_db_write(_delete_zone_db(zone_id)))
    return JSONResponse({"ok": True, "removed": zone_id})


# ── Assets ───────────────────────────────────────────────────

@app.get("/api/assets")
async def api_assets():
    return JSONResponse({"assets": list(STATE["assets"].values()), "server_time": _utc_now_iso()})


@app.post("/api/assets")
async def api_assets_create(req: Request, _=Depends(require_operator())):
    body = await req.json()
    if not body.get("lat") or not body.get("lon") or not body.get("type"):
        return JSONResponse({"ok": False, "error": "lat, lon, type required"}, status_code=400)
    asset_id = body.get("id") or _new_id("asset-")
    asset = {
        "id":         asset_id,
        "name":       body.get("name", asset_id),
        "type":       body.get("type", "unknown"),
        "lat":        float(body["lat"]),
        "lon":        float(body["lon"]),
        "status":     body.get("status", "active"),
        "created_at": _utc_now_iso(),
    }
    async with STATE_LOCK:
        STATE["assets"][asset_id] = asset
    ev = {"event_type": "cop.asset", "payload": asset}
    _append_event_tail(ev)
    await broadcast(ev)
    asyncio.create_task(_db_write(_persist_asset(asset)))
    return JSONResponse({"ok": True, "asset": asset})


@app.delete("/api/assets/{asset_id}")
async def api_assets_delete(asset_id: str, _=Depends(require_operator())):
    async with STATE_LOCK:
        removed = STATE["assets"].pop(asset_id, None)
    if not removed:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    ev = {"event_type": "cop.asset_removed", "payload": {"id": asset_id}}
    _append_event_tail(ev)
    await broadcast(ev)
    asyncio.create_task(_db_write(_delete_asset_db(asset_id)))
    return JSONResponse({"ok": True, "removed": asset_id})


# ── Tasks ────────────────────────────────────────────────────

@app.get("/api/tasks")
async def api_tasks():
    return JSONResponse({"tasks": list(STATE["tasks"].values()), "server_time": _utc_now_iso()})


@app.post("/api/tasks/{task_id}/approve")
async def api_task_approve(task_id: str, req: Request, _=Depends(require_operator())):
    body = await req.json() if req.headers.get("content-length", "0") != "0" else {}
    operator_id = body.get("operator", body.get("operator_id", "operator"))
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

    # ── Fire control loop ──
    # If the approved task was an ENGAGE order, kick off the effector
    # impact sequence on the target track. Runs as a background task so
    # the API call returns immediately — the operator sees the task flip
    # to APPROVED in the UI, then an expanding red circle animation over
    # the target, then the target marker disappears.
    if action == "ENGAGE" and target_id:
        asyncio.create_task(_run_effector_impact(str(target_id), task["id"]))

    return JSONResponse({"ok": True, "task": task})


# ── Fire control / effector pipeline ─────────────────────────

# How long after approval to wait before the impact lands (seconds).
# Represents weapon flight time; also gives the UI a beat to play its
# animation before the target vanishes.
_EFFECTOR_IMPACT_DELAY_S = 2.0


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

    # 2) Simulate weapon flight time.
    await asyncio.sleep(_EFFECTOR_IMPACT_DELAY_S)

    # 3) FSM → DESTROYED. Remove target from state.
    track_fsm.on_destroyed(target_id)
    async with STATE_LOCK:
        STATE["tracks"].pop(target_id, None)
        STATE["threats"].pop(target_id, None)
        AI_PREDICTIONS.pop(target_id, None)
        AI_TRAJECTORIES.pop(target_id, None)
        ai_trajectory.drop_track(target_id)
        AI_ML_PREDICTIONS.pop(target_id, None)

    # 4) Tell the UI to drop the marker.
    await broadcast({
        "event_type": "cop.track_removed",
        "payload": {
            "id":          target_id,
            "reason":      "engaged",
            "task_id":     task_id,
            "server_time": _utc_now_iso(),
        },
    })
    log.info("[fire] engage %s: target %s neutralized", task_id, target_id)


@app.post("/api/tasks/{task_id}/reject")
async def api_task_reject(task_id: str, req: Request, _=Depends(require_operator())):
    body = await req.json() if req.headers.get("content-length", "0") != "0" else {}
    operator_id = body.get("operator", body.get("operator_id", "operator"))
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
    return JSONResponse({"ok": True, "task": task})


# ── Multi-operator: track claims ─────────────────────────────


@app.get("/api/operators")
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


@app.post("/api/tracks/{track_id}/claim")
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
            "server_time": _utc_now_iso(),
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


@app.delete("/api/tracks/{track_id}/claim")
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
            "server_time": _utc_now_iso(),
        },
    }
    await broadcast(ev)
    return JSONResponse({"ok": True, "track_id": track_id})


# ── Waypoints ────────────────────────────────────────────────

@app.get("/api/waypoints")
async def api_waypoints():
    wps = sorted(STATE["waypoints"].values(), key=lambda w: w.get("order", 0))
    return JSONResponse({"waypoints": wps, "server_time": _utc_now_iso()})


@app.post("/api/waypoints")
async def api_waypoints_create(req: Request, _=Depends(require_operator())):
    body = await req.json()
    if body.get("lat") is None or body.get("lon") is None:
        return JSONResponse({"ok": False, "error": "lat and lon required"}, status_code=400)
    wp_id = body.get("id") or _new_id("wp-")
    wp = {
        "id":         wp_id,
        "name":       body.get("name", f"WP-{len(STATE['waypoints']) + 1}"),
        "lat":        float(body["lat"]),
        "lon":        float(body["lon"]),
        "order":      int(body.get("order", len(STATE["waypoints"]))),
        "mission_id": body.get("mission_id", "default"),
        "created_at": _utc_now_iso(),
    }
    async with STATE_LOCK:
        STATE["waypoints"][wp_id] = wp
    ev = {"event_type": "cop.waypoint", "payload": wp}
    _append_event_tail(ev)
    await broadcast(ev)
    asyncio.create_task(_db_write(_persist_waypoint(wp)))
    return JSONResponse({"ok": True, "waypoint": wp})


@app.delete("/api/waypoints/{wp_id}")
async def api_waypoints_delete(wp_id: str, _=Depends(require_operator())):
    async with STATE_LOCK:
        removed = STATE["waypoints"].pop(wp_id, None)
    if not removed:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    ev = {"event_type": "cop.waypoint_removed", "payload": {"id": wp_id}}
    _append_event_tail(ev)
    await broadcast(ev)
    asyncio.create_task(_db_write(_delete_waypoint_db(wp_id)))
    return JSONResponse({"ok": True, "removed": wp_id})


@app.delete("/api/waypoints")
async def api_waypoints_clear(_=Depends(require_operator())):
    async with STATE_LOCK:
        STATE["waypoints"].clear()
    ev = {"event_type": "cop.waypoints_cleared", "payload": {"server_time": _utc_now_iso()}}
    _append_event_tail(ev)
    await broadcast(ev)
    asyncio.create_task(_db_write(_clear_waypoints_db()))
    return JSONResponse({"ok": True})


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
        AI_ML_PREDICTIONS.clear()
        AI_ML_PREV_TRACKS.clear()
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
        cop_sync.reset()
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
    # API key guard: when AUTH_ENABLED and INGEST_API_KEY is set,
    # require X-API-Key header for /ingest access.
    if AUTH_ENABLED and INGEST_API_KEY:
        provided = req.headers.get("x-api-key", "")
        if provided != INGEST_API_KEY:
            METRICS["ingest_bad_request"] += 1
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    # Rate limiting
    client_ip = req.client.host if req.client else "unknown"
    if not _rate_limit_check(client_ip):
        METRICS["ingest_bad_request"] += 1
        return JSONResponse({"ok": False, "error": "rate limited"}, status_code=429)

    # Size guard — reject payloads > 256 KB
    content_length = req.headers.get("content-length")
    if content_length and int(content_length) > 262_144:
        METRICS["ingest_bad_request"] += 1
        return JSONResponse({"ok": False, "error": "payload too large"}, status_code=413)

    try:
        body = await req.json()
    except Exception:
        METRICS["ingest_bad_request"] += 1
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    event_type = body.get("event_type")
    payload    = body.get("payload")

    if not event_type or payload is None:
        METRICS["ingest_bad_request"] += 1
        return JSONResponse({"ok": False, "error": "missing event_type/payload"}, status_code=400)

    # Validate event_type whitelist
    _VALID_EVENT_TYPES = {"cop.track", "cop.threat", "cop.zone", "cop.alert",
                          "cop.asset", "cop.task", "cop.waypoint"}
    if event_type not in _VALID_EVENT_TYPES:
        METRICS["ingest_bad_request"] += 1
        return JSONResponse({"ok": False, "error": f"unknown event_type: {event_type}"}, status_code=400)

    METRICS["ingest_total"] += 1
    METRICS["ingest_by_type"][event_type] = METRICS["ingest_by_type"].get(event_type, 0) + 1

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

                # Track FSM: update lifecycle state
                sensors = payload.get("supporting_sensors", [])
                fsm_state = track_fsm.on_update(str(track_id), sensors)
                payload["track_state"] = fsm_state.value

                STATE["tracks"][str(track_id)] = payload
                lat = payload.get("lat")
                lon = payload.get("lon")
                if lat is not None and lon is not None and STATE["zones"]:
                    await _check_zone_breaches(str(track_id), float(lat), float(lon))

                # ── EW detection: GPS spoofing + false injection ──────────
                if lat is not None and lon is not None:
                    ew_alerts = ai_ew.on_track_update(
                        str(track_id), float(lat), float(lon),
                        sensors=sensors,
                    )
                    for alert in ew_alerts:
                        ai_aar.record_ew_alert(alert)
                        asyncio.create_task(broadcast({
                            "event_type": "cop.ew_alert",
                            "payload":    {**alert, "server_time": _utc_now_iso()},
                        }))

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


# ── Phase 5: AI processing helpers ───────────────────────────
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

_ai_tactical_last = 0.0
_AI_TACTICAL_INTERVAL = 3.0  # run tactical engine every N seconds
_ai_tactical_bg_lock = asyncio.Lock()


def _ai_process_track(track_id: str, lat: float, lon: float, intent: str) -> None:
    """Run predictor + anomaly detection + LSTM trajectory on a single track update."""
    track = STATE["tracks"].get(track_id, {})
    speed   = float(track.get("speed") or track.get("kinematics", {}).get("speed_mps") or 0.0)
    heading = float(track.get("heading") or track.get("kinematics", {}).get("heading_deg") or 0.0)

    # 1) Kalman filter prediction
    preds = ai_predictor.update_track(track_id, lat, lon)
    if preds:
        AI_PREDICTIONS[track_id] = preds

    # 2) LSTM trajectory prediction
    ai_trajectory.update(track_id, lat, lon, speed=speed, heading=heading)
    traj = ai_trajectory.predict(track_id)
    if traj:
        AI_TRAJECTORIES[track_id] = traj

    # 3) Anomaly detection
    anomalies = ai_anomaly.check_track(track_id, lat, lon, intent=intent)
    if anomalies:
        AI_ANOMALIES.extend(anomalies)
        if len(AI_ANOMALIES) > AI_ANOMALY_MAX:
            del AI_ANOMALIES[: len(AI_ANOMALIES) - AI_ANOMALY_MAX]
        # Timeline: record anomaly events
        for a in anomalies:
            ai_timeline.record_anomaly(
                track_id, a.get("type", "UNKNOWN"), a.get("severity", "MEDIUM"),
            )
            ai_aar.record_anomaly(a)


def _ai_run_tactical_compute(
    tracks_snap: Dict[str, Dict],
    threats_snap: Dict[str, Dict],
    assets_snap: Dict[str, Dict],
    zones_snap: Dict[str, Dict],
) -> Dict[str, Any]:
    """
    Pure-compute tactical engine pass. Runs in a thread pool executor so
    it does NOT block the asyncio event loop during heavy ML / analysis.

    Iterates over caller-supplied snapshots (never STATE globals) so it
    is safe against concurrent /ingest mutations. Reads from AI_* globals
    that are only-extended-by-predictor (AI_PREDICTIONS, AI_ANOMALIES,
    AI_ML_PREV_TRACKS) — these are eventually consistent and tolerant to
    stale reads for one tick.

    Returns a dict of results. The caller is responsible for applying
    them to the AI_* globals on the event loop thread.
    """
    import time as _t
    _timings: Dict[str, float] = {}

    # Swarm detection
    _t0 = _t.monotonic()
    swarm_anomalies = ai_anomaly.detect_swarms(tracks_snap)
    _timings["swarm"] = (_t.monotonic() - _t0) * 1000

    # Tactical recommendations
    _t0 = _t.monotonic()
    recs = ai_tactical.generate_recommendations(
        tracks=tracks_snap,
        threats=threats_snap,
        assets=assets_snap,
        zones=zones_snap,
        anomalies=AI_ANOMALIES,
        predictions=AI_PREDICTIONS,
    )
    _timings["tactical"] = (_t.monotonic() - _t0) * 1000

    # Predictive zone breach detection
    _t0 = _t.monotonic()
    breaches = ai_zone_breach.check_predictive_breaches(
        predictions=AI_PREDICTIONS,
        zones=zones_snap,
    )
    _timings["zone_breach"] = (_t.monotonic() - _t0) * 1000

    # Uncertainty cones for frontend
    _t0 = _t.monotonic()
    cones = ai_zone_breach.build_uncertainty_cones(AI_PREDICTIONS)
    _timings["cones"] = (_t.monotonic() - _t0) * 1000

    # Coordinated attack detection
    _t0 = _t.monotonic()
    coord_attacks = ai_coord_attack.detect_coordinated_attacks(
        tracks=tracks_snap,
        predictions=AI_PREDICTIONS,
        zones=zones_snap,
        assets=assets_snap,
    )
    _timings["coord_attack"] = (_t.monotonic() - _t0) * 1000

    # ML threat scoring
    _t0 = _t.monotonic()
    ml_preds: Dict[str, Dict] = {}
    if ai_ml.is_available():
        ml_preds = ai_ml.predict_batch(
            tracks=tracks_snap,
            threats=threats_snap,
            assets=assets_snap,
            zones=zones_snap,
            prev_tracks=AI_ML_PREV_TRACKS,
            dt=_AI_TACTICAL_INTERVAL,
        )
    _timings["ml_threat"] = (_t.monotonic() - _t0) * 1000

    # ROE: engagement advisories
    _t0 = _t.monotonic()
    roe_advs = ai_roe.evaluate_all(
        tracks=tracks_snap,
        threats=threats_snap,
        zones=zones_snap,
        assets=assets_snap,
        coord_attacks=coord_attacks,
    )
    _timings["roe"] = (_t.monotonic() - _t0) * 1000

    # EW: mass jamming detection (periodic check over all track timestamps)
    _t0 = _t.monotonic()
    ew_jamming = ai_ew.check_mass_jamming(tracks_snap)
    _timings["ew"] = (_t.monotonic() - _t0) * 1000

    return {
        "swarm_anomalies":   list(swarm_anomalies),
        "recommendations":   list(recs),
        "pred_breaches":     list(breaches),
        "uncertainty_cones": dict(cones),
        "coord_attacks":     list(coord_attacks),
        "ml_predictions":    ml_preds,
        "roe_advisories":    list(roe_advs),
        "ew_alerts":         list(ew_jamming),
        "_timings_ms":       _timings,
    }


async def _ai_tactical_background_task() -> None:
    """
    Background task: snapshot state, run the tactical engine off-loop in
    an executor, then apply results + broadcast.

    Guarded by ``_ai_tactical_bg_lock`` so at most one pass is in flight.
    If a previous pass is still computing (e.g. ML is slow), this call
    drops the tick entirely — newer state will drive the next tick.
    """
    # Drop this tick if another one is already in flight. Under heavy
    # load this is the right behaviour: no pile-up, always freshest data.
    if _ai_tactical_bg_lock.locked():
        METRICS["tactical_overlap_skipped"] += 1
        return
    async with _ai_tactical_bg_lock:
        # 1) Snapshot state under STATE_LOCK (brief hold, shallow copies).
        async with STATE_LOCK:
            tracks_snap  = dict(STATE["tracks"])
            threats_snap = dict(STATE["threats"])
            assets_snap  = dict(STATE["assets"])
            zones_snap   = dict(STATE["zones"])

        # 2) Run the heavy compute in a thread pool executor so the event
        #    loop stays free to handle /ingest and WebSocket traffic.
        loop = asyncio.get_running_loop()
        t_start = _time_mod.perf_counter()
        try:
            result = await loop.run_in_executor(
                None,
                _ai_run_tactical_compute,
                tracks_snap, threats_snap, assets_snap, zones_snap,
            )
        except Exception as exc:
            METRICS["tactical_failed"] += 1
            log.warning("[cop] tactical compute failed: %s", exc)
            return
        finally:
            _metrics_record_tactical_duration(
                (_time_mod.perf_counter() - t_start) * 1000.0
            )
        METRICS["tactical_ran"] += 1
        if "_timings_ms" in result:
            METRICS["tactical_module_ms"] = result.pop("_timings_ms")

        # 3) Apply results to AI_* globals. We're back on the event loop
        #    thread here, so writes are serialized w.r.t. /ingest handlers.
        if result["swarm_anomalies"]:
            AI_ANOMALIES.extend(result["swarm_anomalies"])
            if len(AI_ANOMALIES) > AI_ANOMALY_MAX:
                del AI_ANOMALIES[: len(AI_ANOMALIES) - AI_ANOMALY_MAX]

        AI_RECOMMENDATIONS.clear()
        AI_RECOMMENDATIONS.extend(result["recommendations"])

        AI_PRED_BREACHES.clear()
        AI_PRED_BREACHES.extend(result["pred_breaches"])

        AI_UNCERTAINTY_CONES.clear()
        AI_UNCERTAINTY_CONES.update(result["uncertainty_cones"])

        AI_COORD_ATTACKS.clear()
        AI_COORD_ATTACKS.extend(result["coord_attacks"])
        for ca in result["coord_attacks"]:
            ai_aar.record_coord_attack(ca)

        if ai_ml.is_available():
            AI_ML_PREDICTIONS.clear()
            AI_ML_PREDICTIONS.update(result["ml_predictions"])
            AI_ML_PREV_TRACKS.clear()
            AI_ML_PREV_TRACKS.update({k: dict(v) for k, v in tracks_snap.items()})

        AI_ROE_ADVISORIES.clear()
        AI_ROE_ADVISORIES.extend(result["roe_advisories"])

        # Broadcast EW jamming alerts individually
        for ew_alert in result.get("ew_alerts", []):
            ai_aar.record_ew_alert(ew_alert)
            await broadcast({
                "event_type": "cop.ew_alert",
                "payload":    {**ew_alert, "server_time": _utc_now_iso()},
            })

        # 4) Broadcast AI update to connected UI clients.
        await broadcast({
            "event_type": "cop.ai_update",
            "payload": {
                "predictions":       AI_PREDICTIONS,
                "anomalies":         AI_ANOMALIES[-20:],
                "recommendations":   AI_RECOMMENDATIONS,
                "pred_breaches":     AI_PRED_BREACHES,
                "uncertainty_cones": AI_UNCERTAINTY_CONES,
                "coord_attacks":     AI_COORD_ATTACKS,
                "roe_advisories":    AI_ROE_ADVISORIES,
                "ml_predictions":    AI_ML_PREDICTIONS,
                "ml_available":      ai_ml.is_available(),
                "server_time":       _utc_now_iso(),
            },
        })


def _schedule_ai_tactical() -> bool:
    """
    Rate-limited scheduler for the tactical background task. Called from
    /ingest. Never blocks: at most it spawns an asyncio task and returns.

    Returns True if a task was scheduled this call, False if skipped due
    to rate limit.
    """
    import time as _time
    global _ai_tactical_last
    METRICS["tactical_scheduled"] += 1
    now = _time.time()
    if now - _ai_tactical_last < _AI_TACTICAL_INTERVAL:
        METRICS["tactical_rate_skipped"] += 1
        return False
    _ai_tactical_last = now
    asyncio.create_task(_ai_tactical_background_task())
    return True


# ── Phase 5: AI API endpoints ───────────────────────────────

@app.get("/api/ai/predictions")
async def api_ai_predictions(track_id: Optional[str] = Query(None)):
    """Get Kalman predicted future positions for tracks."""
    if track_id:
        return JSONResponse({
            "track_id": track_id,
            "predictions": AI_PREDICTIONS.get(track_id, []),
        })
    return JSONResponse({"predictions": {k: v for k, v in AI_PREDICTIONS.items()}})


@app.get("/api/ai/trajectories")
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


@app.get("/api/ai/anomalies")
async def api_ai_anomalies(limit: int = Query(50, le=200)):
    """Get recent anomalies."""
    return JSONResponse({
        "count": len(AI_ANOMALIES),
        "anomalies": AI_ANOMALIES[-limit:],
    })


@app.get("/api/ai/recommendations")
async def api_ai_recommendations():
    """Get current tactical recommendations."""
    return JSONResponse({
        "count": len(AI_RECOMMENDATIONS),
        "recommendations": AI_RECOMMENDATIONS,
    })


@app.get("/api/ai/lineage/{track_id}")
async def api_ai_lineage(track_id: str):
    """Return the full decision lineage chain for a track."""
    chain = ai_lineage.get_chain(track_id)
    summary = ai_lineage.get_summary(track_id)
    return JSONResponse({"track_id": track_id, "summary": summary, "chain": chain})


@app.get("/api/ai/lineage")
async def api_ai_lineage_all():
    """Return lineage stats and all tracked IDs."""
    stats = ai_lineage.stats()
    track_ids = ai_lineage.get_all_track_ids()
    return JSONResponse({"stats": stats, "track_ids": track_ids})


@app.get("/api/ai/briefing")
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


@app.post("/api/ai/chat")
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


@app.post("/api/ai/command")
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


@app.get("/api/ai/pred_breaches")
async def api_ai_pred_breaches():
    """Get predictive zone breach warnings."""
    return JSONResponse({
        "count": len(AI_PRED_BREACHES),
        "breaches": AI_PRED_BREACHES,
    })


@app.get("/api/ai/uncertainty")
async def api_ai_uncertainty(track_id: Optional[str] = Query(None)):
    """Get uncertainty cone data for predicted trajectories."""
    if track_id:
        return JSONResponse({
            "track_id": track_id,
            "cone": AI_UNCERTAINTY_CONES.get(track_id, []),
        })
    return JSONResponse({"cones": AI_UNCERTAINTY_CONES})


@app.get("/api/ai/coordinated")
async def api_ai_coordinated():
    """Get coordinated attack warnings."""
    return JSONResponse({
        "count": len(AI_COORD_ATTACKS),
        "attacks": AI_COORD_ATTACKS,
    })


@app.get("/api/ai/timeline")
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


@app.get("/api/ai/roe")
async def api_ai_roe():
    """Get current ROE engagement advisories."""
    return JSONResponse({
        "count": len(AI_ROE_ADVISORIES),
        "advisories": AI_ROE_ADVISORIES,
    })


@app.get("/api/ai/aar")
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
    return JSONResponse(report)


@app.get("/api/ai/ml")
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


@app.post("/api/ai/ml/train")
async def api_ai_ml_train():
    """Re-train ML model from recordings."""
    try:
        result = ai_ml.train()
        ai_ml.reset()  # force reload of new model
        return JSONResponse({"ok": True, "result": result})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/metrics")
async def api_metrics():
    """
    Runtime performance metrics.

    Exposes ingest counters, tactical engine timings (p50/p95/max),
    WebSocket fan-out stats. Intended for diagnosing performance
    regressions and for the CI smoke test to assert sane numbers.

    Not Prometheus format — just JSON. Single-server system, lightweight.
    """
    recent: List[float] = list(METRICS["tactical_recent_ms"])
    uptime_s = _time_mod.time() - _METRICS_START_TS
    ingest_total = METRICS["ingest_total"]
    return JSONResponse({
        "uptime_s": round(uptime_s, 1),
        "ingest": {
            "total":        ingest_total,
            "per_sec":      round(ingest_total / uptime_s, 2) if uptime_s > 0 else 0.0,
            "by_type":      dict(METRICS["ingest_by_type"]),
            "bad_request":  METRICS["ingest_bad_request"],
        },
        "tactical": {
            "scheduled":        METRICS["tactical_scheduled"],
            "ran":              METRICS["tactical_ran"],
            "rate_skipped":     METRICS["tactical_rate_skipped"],
            "overlap_skipped":  METRICS["tactical_overlap_skipped"],
            "failed":           METRICS["tactical_failed"],
            "last_ms":          METRICS["tactical_last_ms"],
            "max_ms":           METRICS["tactical_max_ms"],
            "p50_ms":           round(_metrics_percentile(recent, 50), 2),
            "p95_ms":           round(_metrics_percentile(recent, 95), 2),
            "sample_count":     len(recent),
            "module_ms":        dict(METRICS.get("tactical_module_ms", {})),
        },
        "websocket": {
            "clients":         len(CLIENTS),
            "broadcasts":      METRICS["ws_broadcasts"],
            "messages_sent":   METRICS["ws_messages_sent"],
            "send_failures":   METRICS["ws_send_failures"],
        },
        "state": {
            "tracks":  len(STATE["tracks"]),
            "threats": len(STATE["threats"]),
            "assets":  len(STATE["assets"]),
            "zones":   len(STATE["zones"]),
            "tasks":   len(STATE["tasks"]),
        },
        "deconfliction": ai_deconfliction.stats(),
        "ew":            ai_ew.stats(),
        "sync":          cop_sync.stats(),
    })


# ── Prometheus text format metrics ────────────────────────────────────────────

@app.get("/metrics", tags=["system"])
async def prometheus_metrics():
    """Prometheus-compatible text metrics (scrape target)."""
    from fastapi.responses import PlainTextResponse
    recent: List[float] = list(METRICS["tactical_recent_ms"])
    uptime_s = _time_mod.time() - _METRICS_START_TS
    ingest_total = METRICS["ingest_total"]
    p50 = _metrics_percentile(recent, 50)
    p95 = _metrics_percentile(recent, 95)

    lines = [
        "# HELP nizam_uptime_seconds Server uptime in seconds",
        "# TYPE nizam_uptime_seconds gauge",
        f"nizam_uptime_seconds {uptime_s:.1f}",
        "",
        "# HELP nizam_ingest_total Total ingested events",
        "# TYPE nizam_ingest_total counter",
        f"nizam_ingest_total {ingest_total}",
        "",
        "# HELP nizam_ingest_per_second Current ingest rate",
        "# TYPE nizam_ingest_per_second gauge",
        f"nizam_ingest_per_second {ingest_total / uptime_s:.2f}" if uptime_s > 0 else "nizam_ingest_per_second 0",
        "",
        "# HELP nizam_ingest_bad_request Bad ingest requests",
        "# TYPE nizam_ingest_bad_request counter",
        f"nizam_ingest_bad_request {METRICS['ingest_bad_request']}",
        "",
        "# HELP nizam_tactical_runs Total tactical engine runs",
        "# TYPE nizam_tactical_runs counter",
        f"nizam_tactical_runs {METRICS['tactical_ran']}",
        "",
        "# HELP nizam_tactical_failed Total tactical engine failures",
        "# TYPE nizam_tactical_failed counter",
        f"nizam_tactical_failed {METRICS['tactical_failed']}",
        "",
        "# HELP nizam_tactical_p50_ms Tactical engine p50 latency ms",
        "# TYPE nizam_tactical_p50_ms gauge",
        f"nizam_tactical_p50_ms {p50:.2f}",
        "",
        "# HELP nizam_tactical_p95_ms Tactical engine p95 latency ms",
        "# TYPE nizam_tactical_p95_ms gauge",
        f"nizam_tactical_p95_ms {p95:.2f}",
        "",
        "# HELP nizam_tactical_max_ms Tactical engine max latency ms",
        "# TYPE nizam_tactical_max_ms gauge",
        f"nizam_tactical_max_ms {METRICS['tactical_max_ms']:.2f}",
        "",
        "# HELP nizam_ws_clients Connected WebSocket clients",
        "# TYPE nizam_ws_clients gauge",
        f"nizam_ws_clients {len(CLIENTS)}",
        "",
        "# HELP nizam_ws_broadcasts Total WS broadcasts",
        "# TYPE nizam_ws_broadcasts counter",
        f"nizam_ws_broadcasts {METRICS['ws_broadcasts']}",
        "",
        "# HELP nizam_tracks Active track count",
        "# TYPE nizam_tracks gauge",
        f"nizam_tracks {len(STATE['tracks'])}",
        "",
        "# HELP nizam_threats Active threat count",
        "# TYPE nizam_threats gauge",
        f"nizam_threats {len(STATE['threats'])}",
        "",
        "# HELP nizam_assets Registered assets",
        "# TYPE nizam_assets gauge",
        f"nizam_assets {len(STATE['assets'])}",
        "",
        "# HELP nizam_zones Defined zones",
        "# TYPE nizam_zones gauge",
        f"nizam_zones {len(STATE['zones'])}",
        "",
        "# HELP nizam_ew_alerts_total Total EW alerts",
        "# TYPE nizam_ew_alerts_total counter",
        f"nizam_ew_alerts_total {ai_ew.stats().get('total_alerts', 0)}",
        "",
        "# HELP nizam_deconfliction_merges Total track merges",
        "# TYPE nizam_deconfliction_merges counter",
        f"nizam_deconfliction_merges {ai_deconfliction.stats().get('total_aliases', 0)}",
    ]
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


# ── Distributed sync endpoints ────────────────────────────────────────────────

@app.post("/api/sync/peers")
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


@app.get("/api/sync/status")
async def api_sync_status():
    """Return current peer sync status."""
    return JSONResponse(cop_sync.stats())


@app.post("/api/sync/receive")
async def api_sync_receive(req: Request):
    """
    Receive a delta snapshot from a peer COP node.
    Applies last-write-wins merge into local STATE.
    """
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
        applied = cop_sync.apply_delta(delta, STATE)

    # Broadcast updated state to connected clients if anything changed
    total_applied = sum(applied.values())
    if total_applied > 0:
        await broadcast({
            "event_type": "cop.sync_applied",
            "payload": {
                "from_node":    node_id,
                "pushed_at":    pushed_at,
                "applied":      applied,
                "server_time":  _utc_now_iso(),
            },
        })
        log.info("[sync] applied %d records from %s", total_applied, node_id)

    return JSONResponse({"ok": True, "applied": applied})


@app.get("/api/ai/status")
async def api_ai_status():
    """AI subsystem status."""
    return JSONResponse({
        "predictions_active": len(AI_PREDICTIONS),
        "anomalies_total": len(AI_ANOMALIES),
        "recommendations_active": len(AI_RECOMMENDATIONS),
        "pred_breaches_active": len(AI_PRED_BREACHES),
        "coord_attacks_active": len(AI_COORD_ATTACKS),
        "roe_advisories_active": len(AI_ROE_ADVISORIES),
        "timeline": ai_timeline.get_summary(),
        "aar": ai_aar.get_status(),
        "recording": replay_recorder.get_status(),
        "ml_model": ai_ml.get_model_info(),
        "llm_enabled": ai_llm.LLM_ENABLED,
        "llm_provider": ai_llm.LLM_PROVIDER if ai_llm.LLM_ENABLED else None,
        "track_fsm": track_fsm.stats(),
        "lstm_trajectory": ai_trajectory.stats(),
    })


# ── Replay API endpoints ─────────────────────────────────────

@app.get("/api/replay/recordings")
async def api_replay_list():
    """List all available recordings."""
    recordings = replay_player.list_recordings()
    return JSONResponse({"recordings": recordings})


@app.post("/api/replay/load")
async def api_replay_load(req: Request):
    """Load a recording for playback."""
    body = await req.json()
    filename = body.get("filename")
    if not filename:
        return JSONResponse({"ok": False, "error": "filename required"}, status_code=400)
    try:
        player = replay_player.get_player()
        info = player.load(filename)
        return JSONResponse({"ok": True, "info": info})
    except (FileNotFoundError, ValueError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)


@app.post("/api/replay/play")
async def api_replay_play(req: Request):
    """Start or resume playback."""
    body = await req.json() if req.headers.get("content-length", "0") != "0" else {}
    speed = float(body.get("speed", 1.0))
    player = replay_player.get_player()
    player.play(speed=speed)
    return JSONResponse({"ok": True, "info": player.get_info()})


@app.post("/api/replay/pause")
async def api_replay_pause():
    """Pause playback."""
    player = replay_player.get_player()
    player.pause()
    return JSONResponse({"ok": True, "info": player.get_info()})


@app.post("/api/replay/stop")
async def api_replay_stop():
    """Stop playback and unload recording."""
    player = replay_player.get_player()
    player.stop()
    return JSONResponse({"ok": True, "info": player.get_info()})


@app.post("/api/replay/seek")
async def api_replay_seek(req: Request):
    """Seek to a specific time."""
    body = await req.json()
    elapsed_s = float(body.get("elapsed_s", 0))
    player = replay_player.get_player()
    player.seek(elapsed_s)
    return JSONResponse({"ok": True, "info": player.get_info()})


@app.post("/api/replay/speed")
async def api_replay_speed(req: Request):
    """Change playback speed."""
    body = await req.json()
    speed = float(body.get("speed", 1.0))
    player = replay_player.get_player()
    player.set_speed(speed)
    return JSONResponse({"ok": True, "info": player.get_info()})


@app.get("/api/replay/frame")
async def api_replay_frame(t: Optional[float] = Query(None)):
    """Get the current (or specified) replay frame."""
    player = replay_player.get_player()
    if player.state == "IDLE":
        return JSONResponse({"ok": False, "error": "no recording loaded"}, status_code=400)
    if t is not None:
        frame = player.get_frame_at(t)
    else:
        frame = player.get_current_frame()
    info = player.get_info()
    return JSONResponse({
        "ok": True,
        "info": info,
        "frame": frame.get("state") if frame else None,
    })


@app.get("/api/replay/status")
async def api_replay_status():
    """Get current replay status."""
    player = replay_player.get_player()
    return JSONResponse({
        "player": player.get_info(),
        "recorder": replay_recorder.get_status(),
    })


# ── Analytics endpoints (Phase 4) ────────────────────────────

@app.get("/api/analytics/tracks")
async def api_analytics_tracks(
    track_id: Optional[str] = Query(None),
    limit:    int           = Query(100, le=5000),
):
    """Query track history from DB. Returns [] when DB not configured."""
    if not DB_ENABLED:
        return JSONResponse({"ok": False, "error": "database not configured"}, status_code=503)
    from sqlalchemy import select, desc
    async with AsyncSessionLocal() as s:
        q = select(TrackEvent).order_by(desc(TrackEvent.time)).limit(limit)
        if track_id:
            q = q.where(TrackEvent.track_id == track_id)
        rows = (await s.execute(q)).scalars().all()
    return JSONResponse({
        "count": len(rows),
        "tracks": [
            {
                "time": r.time.isoformat(), "track_id": r.track_id,
                "lat": r.lat, "lon": r.lon, "altitude": r.altitude,
                "speed": r.speed, "heading": r.heading, "source": r.source,
            }
            for r in rows
        ],
    })


@app.get("/api/analytics/threats")
async def api_analytics_threats(
    track_id: Optional[str] = Query(None),
    limit:    int           = Query(100, le=5000),
):
    if not DB_ENABLED:
        return JSONResponse({"ok": False, "error": "database not configured"}, status_code=503)
    from sqlalchemy import select, desc
    async with AsyncSessionLocal() as s:
        q = select(ThreatEvent).order_by(desc(ThreatEvent.time)).limit(limit)
        if track_id:
            q = q.where(ThreatEvent.track_id == track_id)
        rows = (await s.execute(q)).scalars().all()
    return JSONResponse({
        "count": len(rows),
        "threats": [
            {
                "time": r.time.isoformat(), "track_id": r.track_id,
                "threat_level": r.threat_level, "intent": r.intent,
                "score": r.score, "tti_s": r.tti_s,
            }
            for r in rows
        ],
    })


@app.get("/api/analytics/alerts")
async def api_analytics_alerts(limit: int = Query(100, le=5000)):
    if not DB_ENABLED:
        return JSONResponse({"ok": False, "error": "database not configured"}, status_code=503)
    from sqlalchemy import select, desc
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(AlertRecord).order_by(desc(AlertRecord.time)).limit(limit)
        )).scalars().all()
    return JSONResponse({
        "count": len(rows),
        "alerts": [
            {
                "time": r.time.isoformat(), "track_id": r.track_id,
                "zone_id": r.zone_id, "zone_name": r.zone_name,
                "zone_type": r.zone_type, "lat": r.lat, "lon": r.lon,
            }
            for r in rows
        ],
    })


# ── Scenario CRUD ─────────────────────────────────────────────

_SCENARIOS_DIR = Path(__file__).parent.parent / "scenarios"

@app.get("/api/scenarios", tags=["system"])
async def api_scenarios_list():
    """List all available scenario files."""
    _SCENARIOS_DIR.mkdir(exist_ok=True)
    scenarios = []
    for f in sorted(_SCENARIOS_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            scenarios.append({
                "name":        f.stem,
                "description": data.get("description", ""),
                "duration_s":  data.get("duration_s", 300),
                "rate_hz":     data.get("rate_hz", 1.0),
                "entity_count": len(data.get("entities", [])),
            })
        except Exception:
            scenarios.append({"name": f.stem, "description": "", "duration_s": 300,
                              "rate_hz": 1.0, "entity_count": 0})
    return JSONResponse({"scenarios": scenarios})


@app.get("/api/scenarios/{name}", tags=["system"])
async def api_scenario_get(name: str):
    """Get a specific scenario by name."""
    safe = name.replace("/", "").replace("\\", "").replace("..", "")
    path = _SCENARIOS_DIR / f"{safe}.json"
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")))


@app.post("/api/scenarios", tags=["system"])
async def api_scenario_save(req: Request, _=Depends(require_operator())):
    """Save (create or overwrite) a scenario file."""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    name = body.get("name", "").strip().replace("/", "").replace("\\", "").replace("..", "")
    if not name:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    _SCENARIOS_DIR.mkdir(exist_ok=True)
    path = _SCENARIOS_DIR / f"{name}.json"
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return JSONResponse({"ok": True, "name": name})


@app.delete("/api/scenarios/{name}", tags=["system"])
async def api_scenario_delete(name: str, _=Depends(require_operator())):
    """Delete a scenario file."""
    safe = name.replace("/", "").replace("\\", "").replace("..", "")
    path = _SCENARIOS_DIR / f"{safe}.json"
    if not path.exists():
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    path.unlink()
    return JSONResponse({"ok": True})


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
