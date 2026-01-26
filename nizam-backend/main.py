from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import json
import time
import math
from typing import Set
from collections import deque

app = FastAPI()

# -------------------------------------------------
# CORS (frontend için gerekli)
# -------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------------------------
# GLOBAL STATE
# -------------------------------------------------
STATE = {
    "agents": {},
    "tracks": {},
    "threats": {},
    "paused": False,
    "zone_circle": {
        "lat": 41.015,
        "lon": 28.979,
        "r_m": 500.0
    }
}

# Pause buffer (bounded)
PAUSE_BUFFER = deque(maxlen=1000)

# -------------------------------------------------
# THREAT REASON ENGINE
# -------------------------------------------------
def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())

def in_zone(lat: float, lon: float, zone: dict) -> bool:
    """
    Basit metre hesabı (pratik yaklaşım).
    1 derece lat ~ 111_000 m
    1 derece lon ~ 111_000 * cos(lat) m
    """
    try:
        zlat = float(zone.get("lat"))
        zlon = float(zone.get("lon"))
        r = float(zone.get("r_m"))
        lat = float(lat)
        lon = float(lon)
    except Exception:
        return False

    dy = (lat - zlat) * 111_000.0
    dx = (lon - zlon) * 111_000.0 * max(0.2, abs(math.cos(zlat * math.pi / 180.0)))
    d = (dx * dx + dy * dy) ** 0.5
    return d <= r

def calc_reasons(track: dict) -> list:
    reasons = []

    lat = track.get("lat")
    lon = track.get("lon")
    zone = STATE.get("zone_circle") or {}

    # 1) Zone ihlali
    if lat is not None and lon is not None and zone:
        if in_zone(lat, lon, zone):
            reasons.append("zone_violation")

    # 2) Hızlı yaklaşma: payload içinde speed_mps varsa
    speed = track.get("speed_mps")
    try:
        if speed is not None and float(speed) >= 15.0:
            reasons.append("fast_approach")
    except Exception:
        pass

    return reasons

# -------------------------------------------------
# TRACK CLASSIFICATION (F)
# -------------------------------------------------
def classify_track(track: dict) -> str:
    """
    Basit sınıflandırma (simülasyon):
    - speed_mps >= 20  -> drone
    - speed_mps 5–20   -> vehicle
    - speed_mps < 5    -> human
    - yoksa            -> unknown
    """
    speed = track.get("speed_mps")
    try:
        s = float(speed)
        if s >= 20.0:
            return "drone"
        if s >= 5.0:
            return "vehicle"
        return "human"
    except Exception:
        return "unknown"

# -------------------------------------------------
# WEBSOCKET CLIENTS
# -------------------------------------------------
WS_CLIENTS: Set[WebSocket] = set()

async def ws_broadcast(obj: dict):
    dead = []
    msg = json.dumps(obj)
    for ws in list(WS_CLIENTS):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        WS_CLIENTS.discard(ws)

# -------------------------------------------------
# HTTP ENDPOINTS
# -------------------------------------------------
@app.get("/api/state")
async def get_state():
    return {
        **STATE,
        "buffer_len": len(PAUSE_BUFFER),
        "buffer_max": PAUSE_BUFFER.maxlen
    }

@app.post("/api/zone")
async def set_zone(zone: dict):
    STATE["zone_circle"] = zone
    await ws_broadcast({
        "event_type": "cop.zone",
        "payload": zone
    })
    return {"ok": True}

# -------------------------------
# Pause / Resume
# -------------------------------
@app.post("/api/pause")
async def api_pause():
    STATE["paused"] = True
    await ws_broadcast({
        "event_type": "cop.pause",
        "payload": {
            "paused": True,
            "buffer_len": len(PAUSE_BUFFER),
            "buffer_max": PAUSE_BUFFER.maxlen
        }
    })
    return {"ok": True, "paused": True, "buffer_len": len(PAUSE_BUFFER)}

@app.post("/api/resume")
async def api_resume():
    STATE["paused"] = False

    await ws_broadcast({
        "event_type": "cop.resume",
        "payload": {"paused": False, "flush_start": True, "buffer_len": len(PAUSE_BUFFER)}
    })

    flushed = 0
    while PAUSE_BUFFER:
        evt = PAUSE_BUFFER.popleft()
        await ws_broadcast(evt)
        flushed += 1

    await ws_broadcast({
        "event_type": "cop.resume",
        "payload": {"paused": False, "flush_done": True, "flushed": flushed}
    })
    return {"ok": True, "paused": False, "flushed": flushed}

@app.post("/api/ingest")
async def ingest(event: dict):
    et = event.get("event_type")
    payload = event.get("payload", {})

    # -------- TRACK EVENT --------
    if et == "cop.track":
        tid = payload.get("id")
        if not tid:
            return {"ok": False, "error": "missing id"}

        # enrich payload (Reason + Type)
        payload["last_ts"] = time.time()
        payload["last_update"] = now_iso()
        payload["reasons"] = calc_reasons(payload)
        payload["type"] = classify_track(payload)

        # state update
        STATE["tracks"][tid] = payload

        ws_evt = {"event_type": "cop.track", "payload": payload}

        # Pause logic
        if STATE["paused"]:
            PAUSE_BUFFER.append(ws_evt)
            return {"ok": True, "buffered": True, "buffer_len": len(PAUSE_BUFFER)}
        else:
            await ws_broadcast(ws_evt)
            return {"ok": True, "buffered": False}

    return {"ok": True, "buffered": False}

# -------------------------------------------------
# WEBSOCKET ENDPOINT
# -------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    WS_CLIENTS.add(ws)

    await ws.send_text(json.dumps({
        "event_type": "cop.snapshot",
        "tracks": STATE["tracks"],
        "paused": STATE["paused"],
        "zone_circle": STATE["zone_circle"],
        "buffer_len": len(PAUSE_BUFFER),
        "buffer_max": PAUSE_BUFFER.maxlen
    }))

    try:
        while True:
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    finally:
        WS_CLIENTS.discard(ws)
