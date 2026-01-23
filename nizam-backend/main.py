from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import json
import time
from typing import Dict, Set

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
    return STATE

@app.post("/api/zone")
async def set_zone(zone: dict):
    STATE["zone_circle"] = zone
    await ws_broadcast({
        "event_type": "cop.zone",
        "payload": zone
    })
    return {"ok": True}

@app.post("/api/ingest")
async def ingest(event: dict):
    et = event.get("event_type")
    payload = event.get("payload", {})

    # -------- TRACK EVENT --------
    if et == "cop.track":
        tid = payload.get("id")
        if not tid:
            return {"ok": False, "error": "missing id"}

        # state update
        payload["last_ts"] = time.time()
        STATE["tracks"][tid] = payload

        # WS publish
        await ws_broadcast({
            "event_type": "cop.track",
            "payload": payload
        })

    return {"ok": True, "buffered": False}

# -------------------------------------------------
# WEBSOCKET ENDPOINT
# -------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    WS_CLIENTS.add(ws)

    # İlk bağlanan client’a snapshot gönder
    await ws.send_text(json.dumps({
        "event_type": "cop.snapshot",
        "tracks": STATE["tracks"],
        "paused": STATE["paused"]
    }))

    try:
        while True:
            # Client'tan mesaj beklemiyoruz
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    finally:
        WS_CLIENTS.discard(ws)
