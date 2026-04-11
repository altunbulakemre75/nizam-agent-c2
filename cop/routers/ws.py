"""
cop/routers/ws.py  —  WebSocket /ws endpoint

Extracted from cop/server.py. Handles:
  - JWT auth via ?token= query param (when AUTH_ENABLED)
  - Operator registration and multi-operator state
  - Full state snapshot on connect
  - 10-second heartbeat pings
  - Clean disconnect: release track claims, broadcast operator_left
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from cop.state import (
    STATE, STATE_LOCK, CLIENTS, CLIENTS_LOCK,
    OPERATORS, TRACK_CLAIMS, WS_OPERATORS,
    make_snapshot_payload as _make_snapshot_payload,
)
from cop.ws_broadcast import broadcast
from cop.helpers import utc_now_iso as _utc_now_iso, new_id as _new_id

try:
    from auth.deps import AUTH_ENABLED, _decode_token
except ImportError:
    AUTH_ENABLED = False  # type: ignore
    def _decode_token(token: str) -> str:  # type: ignore
        return token

log = logging.getLogger("nizam.cop")
router = APIRouter()


@router.websocket("/ws")
async def ws_endpoint(
    websocket:   WebSocket,
    token:       Optional[str] = Query(None),
    operator_id: Optional[str] = Query(None),
):
    # Auth check for WebSocket (via query param)
    if AUTH_ENABLED:
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

    released: list = []
    try:
        # Send full state snapshot to this client
        async with STATE_LOCK:
            snapshot = {"event_type": "cop.snapshot", "payload": _make_snapshot_payload()}
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
