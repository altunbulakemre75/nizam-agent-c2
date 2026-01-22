import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse, HTMLResponse

app = FastAPI(title="NIZAM COP", version="0.1")

# -----------------------------
# In-memory state (SENDEKİ YAPI)
# -----------------------------
STATE: Dict[str, Any] = {
    "agents": {},        # agent_key -> {status,last_seen,tags...}
    "tracks": {},        # global_track_id -> latest track.update payload
    "threats": {},       # global_track_id -> latest threat.assessment payload
    "events_tail": [],   # last N events (for debug)
}

EVENT_TAIL_MAX = 500

# -----------------------------
# WS Clients + Locks
# -----------------------------
CLIENTS: Set[WebSocket] = set()
CLIENTS_LOCK = asyncio.Lock()

STATE_LOCK = asyncio.Lock()  # ingest/reset yarışını önler


# -----------------------------
# Helpers
# -----------------------------
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_event_tail(ev: Dict[str, Any]) -> None:
    tail: List[Dict[str, Any]] = STATE["events_tail"]
    tail.append(ev)
    # tail'i max uzunlukta tut
    if len(tail) > EVENT_TAIL_MAX:
        del tail[: len(tail) - EVENT_TAIL_MAX]


async def broadcast(ev: Dict[str, Any]) -> None:
    # Fan-out to all ws clients (best-effort)  (SENİN EKRAN GÖRÜNTÜSÜYLE AYNI)
    dead: List[WebSocket] = []
    async with CLIENTS_LOCK:
        for ws in list(CLIENTS):
            try:
                await ws.send_json(ev)
            except Exception:
                dead.append(ws)
        for ws in dead:
            CLIENTS.discard(ws)


def _make_snapshot_payload() -> Dict[str, Any]:
    # UI snapshot formatı: tracks/threats list olarak bekleniyorsa liste döndür.
    # STATE içinde dict tutuyoruz, dışarı list’e çeviriyoruz.
    tracks_list = list(STATE["tracks"].values())
    threats_list = list(STATE["threats"].values())
    return {
        "tracks": tracks_list,
        "threats": threats_list,
        "server_time": _utc_now_iso(),
    }


# -----------------------------
# Routes
# -----------------------------
@app.get("/", response_class=HTMLResponse)
async def root():
    # Eğer UI'yi burada servis etmek istiyorsan, burayı templates/static ile değiştirebiliriz.
    return "<html><body><h3>NIZAM COP API is running.</h3></body></html>"


@app.get("/api/agents")
async def api_agents():
    return JSONResponse({"agents": STATE["agents"], "server_time": _utc_now_iso()})


@app.get("/api/tracks")
async def api_tracks():
    # UI/Debug kolaylığı için list döndürüyoruz
    return JSONResponse({"tracks": list(STATE["tracks"].values()), "server_time": _utc_now_iso()})


@app.get("/api/threats")
async def api_threats():
    return JSONResponse({"threats": list(STATE["threats"].values()), "server_time": _utc_now_iso()})


@app.get("/api/events_tail")
async def api_events_tail():
    return JSONResponse({"events_tail": STATE["events_tail"], "server_time": _utc_now_iso()})


@app.post("/api/reset")
async def api_reset():
    """
    Gerçek reset:
    - tracks/threats/events_tail temizle
    - boş snapshot broadcast
    """
    async with STATE_LOCK:
        STATE["tracks"].clear()
        STATE["threats"].clear()
        STATE["events_tail"].clear()
        # İstersen agents da reset:
        # STATE["agents"].clear()

        snapshot = {
            "event_type": "cop.snapshot",
            "payload": {
                "tracks": [],
                "threats": [],
                "reset": True,
                "server_time": _utc_now_iso(),
            },
        }
        # tail'e yazmak istersen:
        _append_event_tail({"event_type": "cop.reset", "payload": {"server_time": _utc_now_iso()}})

    await broadcast(snapshot)
    return JSONResponse({"ok": True, "reset": True})


@app.post("/ingest")
async def ingest(req: Request):
    """
    Beklenen:
    {
      "event_type": "cop.track | cop.threat | cop.snapshot | ...",
      "payload": {...}
    }
    """
    body = await req.json()
    event_type = body.get("event_type")
    payload = body.get("payload")

    if not event_type or payload is None:
        return JSONResponse(
            {"ok": False, "error": "missing event_type/payload"},
            status_code=400,
        )

    # Normalleştirme: bazı eventler için server_time ekleyelim
    if isinstance(payload, dict) and "server_time" not in payload:
        payload["server_time"] = _utc_now_iso()

    ev = {"event_type": event_type, "payload": payload}

    async with STATE_LOCK:
        # tail'e yaz
        _append_event_tail(ev)

        # Track/threat state güncelle
        if event_type == "cop.track":
            # id bul: payload içinde id/global_track_id hangisi varsa
            track_id = (
                payload.get("id")
                or payload.get("track_id")
                or payload.get("global_track_id")
                or payload.get("gid")
            )
            if track_id is not None:
                STATE["tracks"][str(track_id)] = payload

        elif event_type == "cop.threat":
            threat_id = (
                payload.get("id")
                or payload.get("track_id")
                or payload.get("global_track_id")
                or payload.get("gid")
            )
            if threat_id is not None:
                STATE["threats"][str(threat_id)] = payload

        elif event_type == "cop.snapshot":
            # Snapshot gelirse (istersen) state'i komple overwrite edebilirsin.
            # UI zaten WS'den dinliyor ama backend de snapshot state tutmak isteyebilir.
            # Şimdilik sadece broadcast edeceğiz.
            pass

    # WS broadcast
    await broadcast(ev)

    return JSONResponse({"ok": True})


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    async with CLIENTS_LOCK:
        CLIENTS.add(websocket)

    # Bağlanır bağlanmaz snapshot gönder (UI için çok iyi olur)
    try:
        async with STATE_LOCK:
            snapshot = {"event_type": "cop.snapshot", "payload": _make_snapshot_payload()}
        await websocket.send_json(snapshot)

        while True:
            # İstemciden mesaj beklemek zorunda değiliz ama bağlantıyı canlı tutar.
            # Client bir şey yollamazsa bu satır bekler.
            await websocket.receive_text()

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        async with CLIENTS_LOCK:
            CLIENTS.discard(websocket)
