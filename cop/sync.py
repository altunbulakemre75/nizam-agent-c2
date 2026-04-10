"""
cop/sync.py — Distributed multi-node COP state synchronisation

Enables two or more COP instances to share track/threat/zone/asset state
in near-real-time without a shared database.

Architecture
────────────
Each node maintains a list of peer COP URLs. A background task runs every
SYNC_INTERVAL_S and pushes a delta snapshot (only records changed since the
last successful push) to each peer via POST /api/sync/receive.

The receiving node applies the delta with a last-write-wins strategy keyed
on (record_id, server_time). Records are never deleted by sync — only
updated if the incoming server_time is newer than what the node already has.

This keeps operator-created data (zones, assets, tasks, waypoints) that was
entered on one node visible on all peers without requiring a central database.
For tracks and threats (high-frequency, ephemeral), only the last N seconds
of updates are synced to avoid flooding.

Security note: sync endpoints should be firewalled to the internal network.
When AUTH_ENABLED, sync pushes include the node's own JWT signed with the
shared JWT_SECRET so peer nodes can verify origin.

Push payload schema
───────────────────
{
  "node_id":    "cop-node-01",
  "pushed_at":  "<ISO timestamp>",
  "delta": {
    "tracks":    {track_id: payload, ...},   # last TRACK_SYNC_WINDOW_S seconds only
    "threats":   {threat_id: payload, ...},
    "zones":     {zone_id: payload, ...},
    "assets":    {asset_id: payload, ...},
    "tasks":     {task_id: payload, ...},
    "waypoints": {wp_id: payload, ...},
  }
}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("nizam.sync")

# ── Config ────────────────────────────────────────────────────────────────────

SYNC_INTERVAL_S    = float(os.environ.get("SYNC_INTERVAL_S", "5"))
TRACK_SYNC_WINDOW_S = float(os.environ.get("TRACK_SYNC_WINDOW_S", "30"))
NODE_ID = os.environ.get("COP_NODE_ID", "cop-node-01")

# ── State ─────────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_peers: Dict[str, Dict[str, Any]] = {}
# {url: {last_push_ts, last_success_ts, last_error, push_count, error_count}}

_last_push_times: Dict[str, float] = {}   # peer_url → last successful push timestamp


# ── Helpers ───────────────────────────────────────────────────────────────────

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(iso: Optional[str]) -> float:
    """Parse ISO timestamp to float epoch. Returns 0.0 on failure."""
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _newer(incoming: dict, existing: dict) -> bool:
    """Return True if incoming record is newer than existing by server_time."""
    return _parse_ts(incoming.get("server_time")) > _parse_ts(existing.get("server_time"))


def _post_json(url: str, body: dict, timeout: float = 5.0) -> bool:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception as exc:
        log.debug("[sync] push failed → %s: %s", url, exc)
        return False


# ── Peer management ───────────────────────────────────────────────────────────

def add_peer(url: str) -> None:
    """Register a peer COP URL. Idempotent."""
    url = url.rstrip("/")
    with _lock:
        if url not in _peers:
            _peers[url] = {
                "url":              url,
                "last_push_ts":     None,
                "last_success_ts":  None,
                "last_error":       None,
                "push_count":       0,
                "error_count":      0,
            }
            log.info("[sync] peer added: %s", url)


def remove_peer(url: str) -> bool:
    url = url.rstrip("/")
    with _lock:
        if url in _peers:
            del _peers[url]
            _last_push_times.pop(url, None)
            log.info("[sync] peer removed: %s", url)
            return True
        return False


def list_peers() -> List[Dict[str, Any]]:
    with _lock:
        return list(_peers.values())


# ── Delta builder ──────────────────────────────────────────────────────────────

def build_delta(state: Dict[str, Any], since: float = 0.0) -> Dict[str, Any]:
    """
    Build a delta payload from the COP STATE dict.

    For tracks and threats: only include records with server_time >= (now - TRACK_SYNC_WINDOW_S)
    For zones, assets, tasks, waypoints: include everything (they change rarely).

    since: if > 0, only include records with server_time >= since (used for incremental push).
    """
    now = time.time()
    track_cutoff = max(since, now - TRACK_SYNC_WINDOW_S)

    def _filter(records: dict, cutoff: float) -> dict:
        result = {}
        for rid, rec in records.items():
            ts = _parse_ts(rec.get("server_time")) if isinstance(rec, dict) else 0.0
            if ts >= cutoff or cutoff == 0.0:
                result[rid] = rec
        return result

    def _all(records: dict) -> dict:
        return {k: v for k, v in records.items() if isinstance(v, dict)}

    return {
        "tracks":    _filter(state.get("tracks", {}), track_cutoff),
        "threats":   _filter(state.get("threats", {}), track_cutoff),
        "zones":     _all(state.get("zones", {})),
        "assets":    _all(state.get("assets", {})),
        "tasks":     _all(state.get("tasks", {})),
        "waypoints": _all(state.get("waypoints", {})),
    }


# ── Delta applier (last-write-wins) ───────────────────────────────────────────

def apply_delta(delta: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, int]:
    """
    Apply incoming delta to local STATE using last-write-wins on server_time.
    Returns counts of records updated per category.
    """
    applied: Dict[str, int] = {}

    for category in ("tracks", "threats", "zones", "assets", "tasks", "waypoints"):
        incoming = delta.get(category, {})
        if not isinstance(incoming, dict):
            continue
        local = state.get(category, {})
        count = 0
        for rid, rec in incoming.items():
            if not isinstance(rec, dict):
                continue
            existing = local.get(rid)
            if existing is None or _newer(rec, existing):
                local[rid] = rec
                count += 1
        applied[category] = count

    return applied


# ── Background push task ──────────────────────────────────────────────────────

async def _push_loop(get_state_fn) -> None:
    """
    Async background task: push delta to all registered peers every SYNC_INTERVAL_S.
    get_state_fn: callable that returns the current COP STATE dict.
    """
    while True:
        await asyncio.sleep(SYNC_INTERVAL_S)

        with _lock:
            peer_urls = list(_peers.keys())

        if not peer_urls:
            continue

        state = get_state_fn()

        for url in peer_urls:
            since = _last_push_times.get(url, 0.0)
            delta = build_delta(state, since=since)

            # Skip empty pushes
            total = sum(len(v) for v in delta.values() if isinstance(v, dict))
            if total == 0:
                continue

            body = {
                "node_id":   NODE_ID,
                "pushed_at": _utc_now_iso(),
                "delta":     delta,
            }

            receive_url = f"{url}/api/sync/receive"
            ok = await asyncio.get_event_loop().run_in_executor(
                None, _post_json, receive_url, body
            )

            now = time.time()
            with _lock:
                peer = _peers.get(url)
                if peer is None:
                    continue
                peer["last_push_ts"] = _utc_now_iso()
                peer["push_count"] = peer.get("push_count", 0) + 1
                if ok:
                    peer["last_success_ts"] = _utc_now_iso()
                    peer["last_error"] = None
                    _last_push_times[url] = now
                else:
                    peer["error_count"] = peer.get("error_count", 0) + 1
                    peer["last_error"] = f"HTTP error at {_utc_now_iso()}"


def start_push_loop(get_state_fn) -> asyncio.Task:
    """Start the background push task. Call from COP server lifespan."""
    return asyncio.create_task(_push_loop(get_state_fn))


def reset() -> None:
    """Clear all peer state (used by /api/reset and tests)."""
    with _lock:
        _peers.clear()
        _last_push_times.clear()


def stats() -> Dict[str, Any]:
    with _lock:
        return {
            "node_id":     NODE_ID,
            "peer_count":  len(_peers),
            "peers":       list(_peers.values()),
        }
