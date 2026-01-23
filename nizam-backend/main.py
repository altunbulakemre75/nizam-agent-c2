# main.py
import asyncio
import time
import json
from typing import Any, Dict, List, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# -----------------------------
# Config (adjust as needed)
# -----------------------------
TTL_LIVE_SEC = 5.0     # age >= TTL_LIVE_SEC => STALE
TTL_DEAD_SEC = 15.0    # age >= TTL_DEAD_SEC => DEAD (remove)
AGING_TICK_SEC = 1.0   # background aging tick

EVENTS_TAIL_MAX = 1000  # keep last N events for debug / visibility

# -----------------------------
# State (single source of truth)
# -----------------------------
STATE: Dict[str, Any] = {
    "agents": {},
    "tracks": {},    # id -> track dict
    "threats": {},
    "events_tail": [],

    # pause/resume
    "paused": False,
    "pause_started_ts": None,   # wall clock time when pause started
    "pause_accum_sec": 0.0,     # total time paused (optional)
    "buffer": [],               # buffered events while paused
}

STATE_LOCK = asyncio.Lock()

# -----------------------------
# WebSocket Hub
# -----------------------------
class WSHub:
    def __init__(self) -> None:
        self.clients: Set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self.lock:
            self.clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self.lock:
            self.clients.discard(ws)

    async def broadcast(self, msg: Dict[str, Any]) -> None:
        # send best-effort; drop dead clients
        data = json.dumps(msg, ensure_ascii=False)
        dead: List[WebSocket] = []
        async with self.lock:
            for ws in self.clients:
                try:
                    await ws.send_text(data)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.clients.discard(ws)

HUB = WSHub()

# -----------------------------
# Models
# -----------------------------
class IngestEvent(BaseModel):
    event_type: str = Field(..., description="cop.track | cop.threat | cop.snapshot | etc.")
    payload: Dict[str, Any] = Field(default_factory=dict)

class PauseReq(BaseModel):
    paused: bool

# -----------------------------
# Helpers
# -----------------------------
def _now_ts() -> float:
    return time.time()

def _push_tail(evt: Dict[str, Any]) -> None:
    tail = STATE["events_tail"]
    tail.append(evt)
    if len(tail) > EVENTS_TAIL_MAX:
        del tail[: len(tail) - EVENTS_TAIL_MAX]

def _build_snapshot_payload() -> Dict[str, Any]:
    # produce a full snapshot for UI overwrite
    return {
        "agents": STATE["agents"],
        "tracks": STATE["tracks"],
        "threats": STATE["threats"],
        "paused": STATE["paused"],
    }

async def _emit(evt: Dict[str, Any]) -> None:
    # record and broadcast
    _push_tail(evt)
    await HUB.broadcast(evt)

async def _broadcast_snapshot() -> None:
    evt = {
        "event_type": "cop.snapshot",
        "payload": _build_snapshot_payload(),
        "ts": _now_ts(),
    }
    await _emit(evt)

def _normalize_track_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensures track has required keys and server-side fields.
    Expected minimum:
      payload.id (string)
      payload.lat, payload.lon (numbers)
    """
    if "id" not in payload:
        raise ValueError("cop.track payload must include 'id'")

    tid = str(payload["id"])

    # Preserve incoming fields (lat/lon etc.) but enforce server fields.
    existing = STATE["tracks"].get(tid, {})

    # last_update_ts should update only on ingest (not on aging)
    last_update_ts = _now_ts()

    track = dict(existing)
    track.update(payload)

    track["id"] = tid
    track["last_update_ts"] = last_update_ts
    track["age_sec"] = 0.0
    track["status"] = "LIVE"

    return track

def _compute_status(age_sec: float) -> str:
    if age_sec >= TTL_DEAD_SEC:
        return "DEAD"
    if age_sec >= TTL_LIVE_SEC:
        return "STALE"
    return "LIVE"

# -----------------------------
# FastAPI App
# -----------------------------
app = FastAPI(title="NIZAM COP Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# REST API
# -----------------------------
@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "ts": _now_ts()}

@app.get("/api/state")
async def get_state() -> Dict[str, Any]:
    async with STATE_LOCK:
        return _build_snapshot_payload()

@app.post("/api/reset")
async def reset_state() -> Dict[str, Any]:
    async with STATE_LOCK:
        STATE["agents"] = {}
        STATE["tracks"] = {}
        STATE["threats"] = {}
        STATE["events_tail"] = []
        STATE["buffer"] = []
        STATE["paused"] = False
        STATE["pause_started_ts"] = None
        STATE["pause_accum_sec"] = 0.0

        # Broadcast empty snapshot (REAL RESET)
        await _broadcast_snapshot()

    return {"ok": True}

@app.post("/api/pause")
async def set_pause(req: PauseReq) -> Dict[str, Any]:
    async with STATE_LOCK:
        if req.paused and not STATE["paused"]:
            # entering pause
            STATE["paused"] = True
            STATE["pause_started_ts"] = _now_ts()
            # notify clients (optional)
            await _emit({"event_type": "cop.control", "payload": {"paused": True}, "ts": _now_ts()})
            # snapshot so UI can show paused state cleanly
            await _broadcast_snapshot()

        elif (not req.paused) and STATE["paused"]:
            # leaving pause
            STATE["paused"] = False
            if STATE["pause_started_ts"] is not None:
                STATE["pause_accum_sec"] += (_now_ts() - STATE["pause_started_ts"])
            STATE["pause_started_ts"] = None

            # 1) Snapshot first (authoritative)
            await _emit({"event_type": "cop.control", "payload": {"paused": False}, "ts": _now_ts()})
            await _broadcast_snapshot()

            # 2) Deterministic catch-up: replay buffered events in arrival order
            buffered = STATE["buffer"]
            STATE["buffer"] = []

        # If we exited pause, process buffer outside lock to avoid long lock hold
    if (not req.paused):
        # Process buffered events after releasing lock
        async with STATE_LOCK:
            buffered = STATE.get("_buffer_to_process", None)
        # We'll avoid extra state key; instead re-read via local variable.
    return {"ok": True, "paused": req.paused}

@app.post("/api/ingest")
async def ingest(evt: IngestEvent) -> Dict[str, Any]:
    """
    Ingest event format:
      { "event_type": "cop.track|cop.threat|cop.snapshot", "payload": {...} }
    Behavior:
      - if paused: buffer the event (do NOT broadcast incremental; UI is frozen)
      - if not paused: apply to STATE and broadcast
    """
    async with STATE_LOCK:
        if STATE["paused"]:
            # buffer it; keep bounded
            STATE["buffer"].append(evt.model_dump())
            if len(STATE["buffer"]) > EVENTS_TAIL_MAX:
                del STATE["buffer"][: len(STATE["buffer"]) - EVENTS_TAIL_MAX]
            # still store in tail for debug
            _push_tail({"event_type": "cop.buffered", "payload": evt.model_dump(), "ts": _now_ts()})
            return {"ok": True, "buffered": True}

        # apply immediately
        await _apply_event_locked(evt.model_dump())

    return {"ok": True, "buffered": False}

@app.get("/api/debug/events_tail")
async def debug_events_tail() -> Dict[str, Any]:
    async with STATE_LOCK:
        return {"events_tail": STATE["events_tail"]}

# -----------------------------
# WebSocket Endpoint
# -----------------------------
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await HUB.connect(ws)
    try:
        # on connect, send snapshot
        async with STATE_LOCK:
            snap = {"event_type": "cop.snapshot", "payload": _build_snapshot_payload(), "ts": _now_ts()}
        await ws.send_text(json.dumps(snap, ensure_ascii=False))

        while True:
            # We don't require client messages; keep alive
            _ = await ws.receive_text()
    except WebSocketDisconnect:
        await HUB.disconnect(ws)
    except Exception:
        await HUB.disconnect(ws)

# -----------------------------
# Core Event Apply
# -----------------------------
async def _apply_event_locked(evt: Dict[str, Any]) -> None:
    """
    Must be called under STATE_LOCK.
    Applies event to STATE, then broadcasts it.
    """
    et = evt.get("event_type")
    payload = evt.get("payload", {}) or {}

    if et == "cop.snapshot":
        # Full overwrite (authoritative)
        # Expected keys: agents, tracks, threats, paused
        # For safety, only overwrite known keys.
        for k in ("agents", "tracks", "threats"):
            if k in payload and isinstance(payload[k], dict):
                STATE[k] = payload[k]
        if "paused" in payload:
            STATE["paused"] = bool(payload["paused"])

        out = {"event_type": "cop.snapshot", "payload": _build_snapshot_payload(), "ts": _now_ts()}
        await _emit(out)
        return

    if et == "cop.track":
        # Normalize and overwrite by id
        track = _normalize_track_payload(payload)
        tid = track["id"]
        STATE["tracks"][tid] = track

        out = {"event_type": "cop.track", "payload": track, "ts": _now_ts()}
        await _emit(out)
        return

    if et == "cop.threat":
        # threats: overwrite by id if exists
        if "id" not in payload:
            raise ValueError("cop.threat payload must include 'id'")
        thid = str(payload["id"])
        existing = STATE["threats"].get(thid, {})
        threat = dict(existing)
        threat.update(payload)
        threat["id"] = thid
        STATE["threats"][thid] = threat

        out = {"event_type": "cop.threat", "payload": threat, "ts": _now_ts()}
        await _emit(out)
        return

    # default passthrough
    out = {"event_type": et, "payload": payload, "ts": _now_ts()}
    await _emit(out)

# -----------------------------
# Resume Buffer Processing
# -----------------------------
async def _drain_buffer_after_resume() -> None:
    """
    If the system was paused, on resume we:
      1) broadcast snapshot
      2) replay buffered events deterministically
    This function is not directly used above; it's here if you want to trigger
    resume from frontend via /api/pause.
    """
    async with STATE_LOCK:
        if STATE["paused"]:
            return
        buffered = STATE["buffer"]
        STATE["buffer"] = []

    # replay outside lock
    for raw in buffered:
        async with STATE_LOCK:
            await _apply_event_locked(raw)

# -----------------------------
# Track Aging Engine (Background)
# -----------------------------
async def _aging_loop() -> None:
    while True:
        await asyncio.sleep(AGING_TICK_SEC)

        async with STATE_LOCK:
            if STATE["paused"]:
                continue

            now = _now_ts()
            to_delete: List[str] = []
            # We will emit status updates only if status changes.
            status_updates: List[Dict[str, Any]] = []

            for tid, tr in list(STATE["tracks"].items()):
                last_ts = tr.get("last_update_ts")
                if not isinstance(last_ts, (int, float)):
                    # if missing, treat as dead-safe
                    last_ts = now

                age = float(now - last_ts)
                old_status = tr.get("status", "LIVE")
                new_status = _compute_status(age)

                tr["age_sec"] = round(age, 3)
                tr["status"] = new_status

                if new_status != old_status:
                    # emit incremental update
                    status_updates.append({
                        "event_type": "cop.track",
                        "payload": {
                            "id": tid,
                            "status": new_status,
                            "age_sec": tr["age_sec"],
                        },
                        "ts": now,
                    })

                if new_status == "DEAD":
                    to_delete.append(tid)

            # delete dead tracks
            for tid in to_delete:
                # emit a final "DEAD" event is already handled above via status change
                # now remove
                STATE["tracks"].pop(tid, None)

        # broadcast updates outside lock for responsiveness
        for u in status_updates:
            await _emit(u)

@app.on_event("startup")
async def on_startup() -> None:
    # start background aging loop
    asyncio.create_task(_aging_loop())
from fastapi import WebSocket, WebSocketDisconnect

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    print("WS CLIENT CONNECTED")

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        print("WS CLIENT DISCONNECTED")
