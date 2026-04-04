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
import urllib.request
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

    yield

    # Stop recording on shutdown
    summary = replay_recorder.stop()
    if summary:
        log.info("[cop] Recording saved: %s (%d frames, %.1fs)",
                 summary["path"], summary["frames"], summary["duration_s"])


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="NIZAM COP", version="0.4", lifespan=lifespan)

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
AI_PREDICTIONS: Dict[str, List[Dict]] = {}   # {track_id: [predicted points]}
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
    async with CLIENTS_LOCK:
        for ws in list(CLIENTS):
            try:
                await ws.send_json(ev)
            except Exception:
                dead.append(ws)
        for ws in dead:
            CLIENTS.discard(ws)


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


def _make_snapshot_payload() -> Dict[str, Any]:
    return {
        "tracks":    list(STATE["tracks"].values()),
        "threats":   list(STATE["threats"].values()),
        "zones":     list(STATE["zones"].values()),
        "assets":    list(STATE["assets"].values()),
        "tasks":     [t for t in STATE["tasks"].values() if t["status"] == "PENDING"],
        "waypoints": list(STATE["waypoints"].values()),
        "predictions":       AI_PREDICTIONS,
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
    async with STATE_LOCK:
        task = STATE["tasks"].get(task_id)
        if not task:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        task["status"]      = "APPROVED"
        task["resolved_at"] = _utc_now_iso()
        task["resolved_by"] = body.get("operator", "operator")
    ev = {"event_type": "cop.task_update", "payload": dict(task)}
    _append_event_tail(ev)
    await broadcast(ev)
    asyncio.create_task(_db_write(_persist_task_update(task)))
    return JSONResponse({"ok": True, "task": task})


@app.post("/api/tasks/{task_id}/reject")
async def api_task_reject(task_id: str, req: Request, _=Depends(require_operator())):
    body = await req.json() if req.headers.get("content-length", "0") != "0" else {}
    async with STATE_LOCK:
        task = STATE["tasks"].get(task_id)
        if not task:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        task["status"]      = "REJECTED"
        task["resolved_at"] = _utc_now_iso()
        task["resolved_by"] = body.get("operator", "operator")
    ev = {"event_type": "cop.task_update", "payload": dict(task)}
    _append_event_tail(ev)
    await broadcast(ev)
    asyncio.create_task(_db_write(_persist_task_update(task)))
    return JSONResponse({"ok": True, "task": task})


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
        AI_PREDICTIONS.clear()
        AI_ANOMALIES.clear()
        AI_RECOMMENDATIONS.clear()
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

@app.post("/ingest")
async def ingest(req: Request):
    body       = await req.json()
    event_type = body.get("event_type")
    payload    = body.get("payload")

    if not event_type or payload is None:
        return JSONResponse({"ok": False, "error": "missing event_type/payload"}, status_code=400)

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
                STATE["tracks"][str(track_id)] = payload
                lat = payload.get("lat")
                lon = payload.get("lon")
                if lat is not None and lon is not None and STATE["zones"]:
                    await _check_zone_breaches(str(track_id), float(lat), float(lon))

                # ── Phase 5: AI hooks on track update ──
                if lat is not None and lon is not None:
                    _ai_process_track(str(track_id), float(lat), float(lon),
                                      payload.get("intent", "unknown"))
                # AAR: record track
                ai_aar.record_track(str(track_id), len(STATE["tracks"]))

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
            asyncio.create_task(_db_write(_persist_threat(payload)))

        elif event_type == "cop.snapshot":
            pass

    # ── Phase 5: periodic tactical analysis (every ingest) ──
    _ai_run_tactical()

    # ── Record frame for replay ──
    replay_recorder.capture_frame(_make_snapshot_payload)

    await broadcast(ev)
    return JSONResponse({"ok": True})


# ── Phase 5: AI processing helpers ───────────────────────────

_ai_tactical_last = 0.0
_AI_TACTICAL_INTERVAL = 3.0  # run tactical engine every N seconds


def _ai_process_track(track_id: str, lat: float, lon: float, intent: str) -> None:
    """Run predictor + anomaly detection on a single track update."""
    # 1) Kalman filter prediction
    preds = ai_predictor.update_track(track_id, lat, lon)
    if preds:
        AI_PREDICTIONS[track_id] = preds

    # 2) Anomaly detection
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


def _ai_run_tactical() -> None:
    """Run tactical recommendation engine (rate-limited)."""
    import time as _time
    global _ai_tactical_last
    now = _time.time()
    if now - _ai_tactical_last < _AI_TACTICAL_INTERVAL:
        return
    _ai_tactical_last = now

    # Swarm detection
    swarm_anomalies = ai_anomaly.detect_swarms(STATE["tracks"])
    if swarm_anomalies:
        AI_ANOMALIES.extend(swarm_anomalies)
        if len(AI_ANOMALIES) > AI_ANOMALY_MAX:
            del AI_ANOMALIES[: len(AI_ANOMALIES) - AI_ANOMALY_MAX]

    # Tactical recommendations
    recs = ai_tactical.generate_recommendations(
        tracks=STATE["tracks"],
        threats=STATE["threats"],
        assets=STATE["assets"],
        zones=STATE["zones"],
        anomalies=AI_ANOMALIES,
        predictions=AI_PREDICTIONS,
    )
    AI_RECOMMENDATIONS.clear()
    AI_RECOMMENDATIONS.extend(recs)

    # Predictive zone breach detection
    breaches = ai_zone_breach.check_predictive_breaches(
        predictions=AI_PREDICTIONS,
        zones=STATE["zones"],
    )
    AI_PRED_BREACHES.clear()
    AI_PRED_BREACHES.extend(breaches)

    # Uncertainty cones for frontend
    cones = ai_zone_breach.build_uncertainty_cones(AI_PREDICTIONS)
    AI_UNCERTAINTY_CONES.clear()
    AI_UNCERTAINTY_CONES.update(cones)

    # Coordinated attack detection
    coord_attacks = ai_coord_attack.detect_coordinated_attacks(
        tracks=STATE["tracks"],
        predictions=AI_PREDICTIONS,
        zones=STATE["zones"],
        assets=STATE["assets"],
    )
    AI_COORD_ATTACKS.clear()
    AI_COORD_ATTACKS.extend(coord_attacks)
    # AAR: record coordinated attacks
    for ca in coord_attacks:
        ai_aar.record_coord_attack(ca)

    # ML threat scoring
    if ai_ml.is_available():
        ml_preds = ai_ml.predict_batch(
            tracks=STATE["tracks"],
            threats=STATE["threats"],
            assets=STATE["assets"],
            zones=STATE["zones"],
            prev_tracks=AI_ML_PREV_TRACKS,
            dt=_AI_TACTICAL_INTERVAL,
        )
        AI_ML_PREDICTIONS.clear()
        AI_ML_PREDICTIONS.update(ml_preds)
        AI_ML_PREV_TRACKS.clear()
        AI_ML_PREV_TRACKS.update({k: dict(v) for k, v in STATE["tracks"].items()})

    # ROE: engagement advisories
    roe_advs = ai_roe.evaluate_all(
        tracks=STATE["tracks"],
        threats=STATE["threats"],
        zones=STATE["zones"],
        assets=STATE["assets"],
        coord_attacks=coord_attacks,
    )
    AI_ROE_ADVISORIES.clear()
    AI_ROE_ADVISORIES.extend(roe_advs)


# ── Phase 5: AI API endpoints ───────────────────────────────

@app.get("/api/ai/predictions")
async def api_ai_predictions(track_id: Optional[str] = Query(None)):
    """Get predicted future positions for tracks."""
    if track_id:
        return JSONResponse({
            "track_id": track_id,
            "predictions": AI_PREDICTIONS.get(track_id, []),
        })
    return JSONResponse({"predictions": {k: v for k, v in AI_PREDICTIONS.items()}})


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


# ── WebSocket ─────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(
    websocket: WebSocket,
    token:     Optional[str] = Query(None),
):
    # Auth check for WebSocket (via query param)
    if AUTH_ENABLED:
        from auth.deps import _decode_token
        username = _decode_token(token or "")
        if not username:
            await websocket.close(code=4001)
            return

    await websocket.accept()
    async with CLIENTS_LOCK:
        CLIENTS.add(websocket)

    try:
        async with STATE_LOCK:
            snapshot = {"event_type": "cop.snapshot", "payload": _make_snapshot_payload()}
        await websocket.send_json(snapshot)

        while True:
            await asyncio.sleep(60)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        async with CLIENTS_LOCK:
            CLIENTS.discard(websocket)
