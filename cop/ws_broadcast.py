"""
cop/ws_broadcast.py  —  WebSocket fan-out helper

Extracted from cop/server.py so routers can `from cop.ws_broadcast import
broadcast` without pulling in all of server.py. The function mutates the
CLIENTS set (removes dead sockets) and bumps the METRICS counters — both
of which live in cop/state.py, so this module has no circular dependency
on server.py.
"""
from __future__ import annotations

from typing import Any, Dict, List

from starlette.websockets import WebSocket

from cop.state import CLIENTS, CLIENTS_LOCK, METRICS, STATE, EVENT_TAIL_MAX


def append_event_tail(ev: Dict[str, Any]) -> None:
    """Append an event to STATE["events_tail"], capped at EVENT_TAIL_MAX.

    Most handlers call this immediately before broadcast() so the events
    are both persisted in the short rolling window and fanned out to
    clients. They remain separate functions because a few handlers
    broadcast without recording (e.g. cop.ping heartbeats).
    """
    tail: List[Dict[str, Any]] = STATE["events_tail"]
    tail.append(ev)
    if len(tail) > EVENT_TAIL_MAX:
        del tail[: len(tail) - EVENT_TAIL_MAX]


async def broadcast(ev: Dict[str, Any]) -> None:
    """Fan-out a JSON payload to every connected WebSocket client.

    Dead clients (those whose send_json raises) are evicted from CLIENTS
    and counted in METRICS["ws_send_failures"]. Safe to call from any
    async context; takes CLIENTS_LOCK for the duration of the send.
    """
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
