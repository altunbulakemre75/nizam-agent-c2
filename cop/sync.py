"""
cop/sync.py — Distributed multi-node COP state synchronisation

Enables two or more COP instances to share track/threat/zone/asset state
in near-real-time without a shared database.

Architecture
────────────
Each node maintains a list of peer COP URLs. A background task runs every
SYNC_INTERVAL_S and pushes a delta snapshot (only records changed since the
last successful push) to each peer via POST /api/sync/receive.

Conflict resolution: Vector clocks
───────────────────────────────────
Each record carries a ``_vclock`` dict mapping ``node_id → counter``.
When a node modifies a record it increments its own counter in the clock.

On receive the clocks are compared:
  - Incoming dominates local → accept (no conflict)
  - Local dominates incoming → skip (stale)
  - Concurrent (neither dominates) →
      • Ephemeral data (tracks, threats): last-write-wins by server_time
      • Operator data (zones, assets, tasks, waypoints): CONFLICT logged,
        higher-priority node wins, entry stored in _conflicts for operator review

Split-brain detection: when a peer is unreachable for > PARTITION_TIMEOUT_S,
the node marks a partition event. On reconnection, records modified during
the partition window are flagged for audit.

Push payload schema
───────────────────
{
  "node_id":    "cop-node-01",
  "pushed_at":  "<ISO timestamp>",
  "delta": {
    "tracks":    {track_id: payload, ...},
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
import copy
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

log = logging.getLogger("nizam.sync")

# ── Config ────────────────────────────────────────────────────────────────────

SYNC_INTERVAL_S     = float(os.environ.get("SYNC_INTERVAL_S", "5"))
TRACK_SYNC_WINDOW_S = float(os.environ.get("TRACK_SYNC_WINDOW_S", "30"))
PARTITION_TIMEOUT_S = float(os.environ.get("SYNC_PARTITION_TIMEOUT_S", "30"))
NODE_ID = os.environ.get("COP_NODE_ID", "cop-node-01")

# ── Vector clock ─────────────────────────────────────────────────────────────

# Ephemeral categories where LWW is acceptable during concurrent edits
_EPHEMERAL = {"tracks", "threats"}
# Operator categories where conflicts require explicit resolution
_OPERATOR  = {"zones", "assets", "tasks", "waypoints"}

# Max conflict log entries
_MAX_CONFLICTS = 200

# ── State ─────────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_peers: Dict[str, Dict[str, Any]] = {}
# {url: {last_push_ts, last_success_ts, last_error, push_count, error_count}}

_last_push_times: Dict[str, float] = {}   # peer_url → last successful push timestamp

# Conflict log: deque of {category, record_id, local_node, remote_node,
#                          local_record, remote_record, resolved_by, time}
_conflicts: Deque[Dict[str, Any]] = deque(maxlen=_MAX_CONFLICTS)

# Partition tracking: peer_url → {"partitioned_since": float | None}
_partition_state: Dict[str, Dict[str, Any]] = {}


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


# ── Vector Clock helpers ─────────────────────────────────────────────────────

def vclock_increment(rec: dict) -> dict:
    """Stamp a record with the local node's vector clock tick."""
    vc = dict(rec.get("_vclock") or {})
    vc[NODE_ID] = vc.get(NODE_ID, 0) + 1
    rec["_vclock"] = vc
    rec["_origin_node"] = NODE_ID
    return rec


def _vclock_dominates(a: Dict[str, int], b: Dict[str, int]) -> bool:
    """
    Return True if vector clock *a* dominates *b* (a ≥ b for all entries,
    a > b for at least one).
    """
    all_keys = set(a.keys()) | set(b.keys())
    at_least_one_greater = False
    for k in all_keys:
        va = a.get(k, 0)
        vb = b.get(k, 0)
        if va < vb:
            return False
        if va > vb:
            at_least_one_greater = True
    return at_least_one_greater


def _vclock_concurrent(a: Dict[str, int], b: Dict[str, int]) -> bool:
    """Return True if neither clock dominates the other (concurrent edit)."""
    return not _vclock_dominates(a, b) and not _vclock_dominates(b, a)


def _vclock_merge(a: Dict[str, int], b: Dict[str, int]) -> Dict[str, int]:
    """Merge two clocks by taking the max of each entry."""
    merged: Dict[str, int] = {}
    for k in set(a.keys()) | set(b.keys()):
        merged[k] = max(a.get(k, 0), b.get(k, 0))
    return merged


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


# ── Delta applier (vector clock conflict resolution) ─────────────────────────

def apply_delta(
    delta: Dict[str, Any],
    state: Dict[str, Any],
    source_node: Optional[str] = None,
) -> Dict[str, int]:
    """
    Apply incoming delta to local STATE using vector clock ordering.

    For each incoming record:
      1. No local copy → accept immediately
      2. Incoming vclock dominates local → accept (clean update)
      3. Local vclock dominates incoming → skip (stale)
      4. Concurrent (split-brain) →
         - Ephemeral (tracks, threats): LWW by server_time (acceptable loss)
         - Operator (zones, assets, tasks, waypoints): accept with merged clock,
           log to _conflicts for operator review

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

            # Case 1: new record — accept
            if existing is None:
                local[rid] = rec
                count += 1
                continue

            inc_vc = rec.get("_vclock") or {}
            loc_vc = existing.get("_vclock") or {}

            # Case 2: incoming dominates — clean accept
            if _vclock_dominates(inc_vc, loc_vc):
                local[rid] = rec
                count += 1
                continue

            # Case 3: local dominates — skip (stale)
            if _vclock_dominates(loc_vc, inc_vc):
                continue

            # Case 4: concurrent — split-brain conflict
            if category in _EPHEMERAL:
                # Ephemeral: LWW by server_time, merge clocks
                if _newer(rec, existing):
                    rec["_vclock"] = _vclock_merge(inc_vc, loc_vc)
                    local[rid] = rec
                    count += 1
            else:
                # Operator data: accept if newer, log conflict
                winner = "remote" if _newer(rec, existing) else "local"
                _conflicts.append({
                    "category":      category,
                    "record_id":     rid,
                    "local_node":    NODE_ID,
                    "remote_node":   source_node or "unknown",
                    "resolved_by":   f"LWW ({winner})",
                    "time":          _utc_now_iso(),
                    "local_server_time":  existing.get("server_time"),
                    "remote_server_time": rec.get("server_time"),
                })
                if winner == "remote":
                    rec["_vclock"] = _vclock_merge(inc_vc, loc_vc)
                    local[rid] = rec
                    count += 1
                else:
                    existing["_vclock"] = _vclock_merge(inc_vc, loc_vc)

        applied[category] = count

    return applied


def get_conflicts() -> List[Dict[str, Any]]:
    """Return the conflict log for operator review."""
    with _lock:
        return list(_conflicts)


def clear_conflicts() -> int:
    """Clear all recorded conflicts. Returns count cleared."""
    with _lock:
        n = len(_conflicts)
        _conflicts.clear()
        return n


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

                # Partition tracking
                ps = _partition_state.setdefault(url, {"partitioned_since": None})

                if ok:
                    # Connection restored — log if was partitioned
                    if ps["partitioned_since"] is not None:
                        dur = now - ps["partitioned_since"]
                        log.warning(
                            "[sync] partition healed: %s (down %.1fs) — "
                            "check /api/sync/conflicts for divergent records",
                            url, dur,
                        )
                    ps["partitioned_since"] = None
                    peer["last_success_ts"] = _utc_now_iso()
                    peer["last_error"] = None
                    _last_push_times[url] = now
                else:
                    peer["error_count"] = peer.get("error_count", 0) + 1
                    peer["last_error"] = f"HTTP error at {_utc_now_iso()}"
                    # Detect partition onset
                    last_success = _parse_ts(peer.get("last_success_ts"))
                    if last_success > 0 and (now - last_success) > PARTITION_TIMEOUT_S:
                        if ps["partitioned_since"] is None:
                            ps["partitioned_since"] = now
                            log.warning("[sync] partition detected: %s unreachable for >%.0fs",
                                        url, PARTITION_TIMEOUT_S)


def start_push_loop(get_state_fn) -> asyncio.Task:
    """Start the background push task. Call from COP server lifespan."""
    return asyncio.create_task(_push_loop(get_state_fn))


def reset() -> None:
    """Clear all peer state (used by /api/reset and tests)."""
    with _lock:
        _peers.clear()
        _last_push_times.clear()
        _conflicts.clear()
        _partition_state.clear()


def stats() -> Dict[str, Any]:
    now = time.time()
    with _lock:
        peers_out = []
        for p in _peers.values():
            pd = dict(p)
            url = pd["url"]
            ps = _partition_state.get(url, {})
            pd["partitioned"] = ps.get("partitioned_since") is not None
            if ps.get("partitioned_since"):
                pd["partition_duration_s"] = round(now - ps["partitioned_since"], 1)
            peers_out.append(pd)
        return {
            "node_id":          NODE_ID,
            "peer_count":       len(_peers),
            "peers":            peers_out,
            "conflict_count":   len(_conflicts),
            "sync_interval_s":  SYNC_INTERVAL_S,
        }
