import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI(title="NIZAM COP", version="0.1")

# -----------------------------
# Templates + Static (UI serve)
# -----------------------------
templates = Jinja2Templates(directory="cop/templates")
app.mount("/static", StaticFiles(directory="cop/static"), name="static")

# -----------------------------
# In-memory state
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


def _make_snapshot_payload() -> Dict[str, Any]:
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
async def root(request: Request):
    # UI entrypoint
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/agents")
async def api_agents():
    return JSONResponse({"agents": STATE["agents"], "server_time": _utc_now_iso()})


@app.get("/api/tracks")
async def api_tracks():
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

    # server_time ekle
    if isinstance(payload, dict) and "server_time" not in payload:
        payload["server_time"] = _utc_now_iso()

    ev = {"event_type": event_type, "payload": payload}

    async with STATE_LOCK:
        _append_event_tail(ev)

        if event_type == "cop.track":
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
            # İstersen snapshot ile full overwrite:
            # STATE["tracks"] = {str(t["id"]): t for t in payload.get("tracks", []) if isinstance(t, dict) and "id" in t}
            # STATE["threats"] = {str(x["id"]): x for x in payload.get("threats", []) if isinstance(x, dict) and "id" in x}
            pass

    await broadcast(ev)
    return JSONResponse({"ok": True})


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    async with CLIENTS_LOCK:
        CLIENTS.add(websocket)

    try:
        # İlk snapshot
        async with STATE_LOCK:
            snapshot = {"event_type": "cop.snapshot", "payload": _make_snapshot_payload()}
        await websocket.send_json(snapshot)

        # Keep-alive loop (client mesaj göndermese bile bağlantı açık kalsın)
        while True:
            await asyncio.sleep(60)
            # İstersen ping gibi heartbeat yollayabilirsin:
            # await websocket.send_json({"event_type": "cop.heartbeat", "payload": {"server_time": _utc_now_iso()}})

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        async with CLIENTS_LOCK:
            CLIENTS.discard(websocket)
