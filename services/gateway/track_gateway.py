"""Track Gateway — NATS `nizam.tracks.active` → WebSocket yayını.

Vite dev server `/ws` yolunu `ws://localhost:8200` adresine proxy'ler,
yani frontend `ws://localhost:5173/ws/tracks` adresiyle bağlanır.
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from prometheus_client import Counter, Gauge, generate_latest
from starlette.responses import Response

if TYPE_CHECKING:
    import nats.aio.msg

NATS_URL = "nats://localhost:6222"
NATS_SUBJECT = "nizam.tracks.active"

_broadcasts = Counter("nizam_gw_broadcasts_total", "WebSocket'a yollanan mesaj sayısı")
_clients_gauge = Gauge("nizam_gw_ws_clients", "Bağlı WebSocket istemci sayısı")


class ConnectionHub:
    """Bağlı WebSocket istemcilerini takip eder ve broadcast yapar."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._snapshot: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
            _clients_gauge.set(len(self._clients))
        # bağlanır bağlanmaz mevcut snapshot gönder
        await ws.send_text(
            json.dumps({"type": "snapshot", "tracks": list(self._snapshot.values())})
        )

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)
            _clients_gauge.set(len(self._clients))

    async def broadcast_track(self, track: dict) -> None:
        tid = track.get("track_id")
        if not tid:
            return
        state = track.get("state")
        if state == "deleted":
            self._snapshot.pop(tid, None)
            payload = json.dumps({"type": "remove", "track_id": tid})
        else:
            self._snapshot[tid] = track
            payload = json.dumps({"type": "track", "track": track})
        await self._send_all(payload)

    async def _send_all(self, payload: str) -> None:
        dead: list[WebSocket] = []
        async with self._lock:
            clients = list(self._clients)
        for ws in clients:
            try:
                await ws.send_text(payload)
                _broadcasts.inc()
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)
                _clients_gauge.set(len(self._clients))


hub = ConnectionHub()


async def _nats_consumer(hub_: ConnectionHub) -> None:
    import nats

    nc = await nats.connect(NATS_URL)

    async def handler(msg: "nats.aio.msg.Msg") -> None:
        try:
            track = json.loads(msg.data.decode())
        except json.JSONDecodeError:
            return
        await hub_.broadcast_track(track)

    await nc.subscribe(NATS_SUBJECT, cb=handler)
    while True:
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    task = asyncio.create_task(_nats_consumer(hub))
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="NIZAM Track Gateway", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type="text/plain; version=0.0.4")


def _auth_required() -> bool:
    """NIZAM_WS_AUTH_DISABLED=true → dev modu. Üretimde true VERİLMEMELİ."""
    return os.getenv("NIZAM_WS_AUTH_DISABLED", "false").lower() != "true"


@app.websocket("/ws/tracks")
async def ws_tracks(ws: WebSocket, token: str | None = None) -> None:
    """JWT auth: ?token=<JWT> query param veya Authorization header.

    NIZAM_JWT_SECRET set edilmeli. Token invalid/expired → 4401 close.
    """
    if _auth_required():
        from shared.auth import AuthError, verify_token

        raw = token
        if not raw:
            header = ws.headers.get("authorization", "")
            if header.lower().startswith("bearer "):
                raw = header[7:]
        if not raw:
            await ws.close(code=4401, reason="missing token")
            return
        try:
            verify_token(raw)
        except AuthError as exc:
            await ws.close(code=4401, reason=f"auth: {exc}")
            return

    await hub.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await hub.disconnect(ws)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8200, log_level="info")


if __name__ == "__main__":
    main()
