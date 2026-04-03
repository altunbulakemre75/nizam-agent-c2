"""
cop/server.py  —  NIZAM COP  (Phase 4)
Phases 1-3: tracks, threats, zones, alerts, assets, tasks, waypoints
Phase 4   : PostgreSQL/TimescaleDB persistence + JWT auth (optional)

ENV:
  DATABASE_URL      postgresql+asyncpg://user:pass@host:5432/nizam
  AUTH_ENABLED      true | false (default false)
  JWT_SECRET        change in production
  ORCHESTRATOR_URL  http://127.0.0.1:8200
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

    yield


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


def _make_snapshot_payload() -> Dict[str, Any]:
    return {
        "tracks":    list(STATE["tracks"].values()),
        "threats":   list(STATE["threats"].values()),
        "zones":     list(STATE["zones"].values()),
        "assets":    list(STATE["assets"].values()),
        "tasks":     [t for t in STATE["tasks"].values() if t["status"] == "PENDING"],
        "waypoints": list(STATE["waypoints"].values()),
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
            asyncio.create_task(_db_write(_persist_track(payload)))

        elif event_type == "cop.threat":
            threat_id = (
                payload.get("id") or payload.get("track_id")
                or payload.get("global_track_id") or payload.get("gid")
            )
            if threat_id is not None:
                STATE["threats"][str(threat_id)] = payload
                await _auto_task(str(threat_id), payload)
            asyncio.create_task(_db_write(_persist_threat(payload)))

        elif event_type == "cop.snapshot":
            pass

    await broadcast(ev)
    return JSONResponse({"ok": True})


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
