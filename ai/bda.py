"""
ai/bda.py — Battle Damage Assessment (BDA) tracker

Records the outcome of every approved ENGAGE task:
  DESTROYED      — hit confirmed, track removed
  MISS           — weapons fired but outcome pending (track may have survived)
  EVADED         — miss confirmed: track still alive 30 s after engagement
  DESTROYED_LATE — miss-pending track disappeared (fusion drop / fled AOI)

Usage (from cop/server.py):
    hit = ai_bda.roll_outcome(task_id, track_id, action, operator, ts)
    # if hit → track should be removed normally
    # if miss → keep track in STATE, BDA monitor will resolve later

    # in a background loop (every 10 s):
    finalized = ai_bda.check_pending(set(STATE["tracks"].keys()))
    for rec in finalized:
        await broadcast({"event_type": "cop.bda", "payload": rec})
"""
from __future__ import annotations

import random
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Set

# ── Configuration ─────────────────────────────────────────────────────────────

HIT_PROBABILITY: float = 0.75     # P(hit) for a clean ENGAGE order
BDA_MONITOR_DELAY_S: float = 30.0  # seconds after miss to check track liveness

# ── Module state ──────────────────────────────────────────────────────────────

_BDA_RECORDS: List[Dict[str, Any]] = []          # finalized records
_PENDING_MISS: Dict[str, Dict[str, Any]] = {}    # task_id → pending record


# ── Public API ─────────────────────────────────────────────────────────────────

def roll_outcome(
    task_id:        str,
    track_id:       str,
    action:         str,
    operator:       str,
    engaged_at:     str,
    hit_probability: float = HIT_PROBABILITY,
) -> bool:
    """
    Probabilistically determine engagement outcome.

    Registers the result internally and returns True (hit) or False (miss).
    Caller should remove the track from STATE only when this returns True.
    """
    hit = random.random() < hit_probability
    rec: Dict[str, Any] = {
        "bda_id":       f"bda-{uuid.uuid4().hex[:8]}",
        "task_id":      task_id,
        "track_id":     track_id,
        "action":       action,
        "operator":     operator,
        "engaged_at":   engaged_at,
        "outcome":      "DESTROYED" if hit else "MISS",
        "confirmed_at": engaged_at if hit else None,
    }
    if hit:
        _BDA_RECORDS.append(rec)
    else:
        # Store with a monotonic deadline for later liveness check
        _PENDING_MISS[task_id] = {
            **rec,
            "_check_after": time.monotonic() + BDA_MONITOR_DELAY_S,
        }
    return hit


def check_pending(alive_track_ids: Set[str]) -> List[Dict[str, Any]]:
    """
    Finalize pending MISS records whose check deadline has passed.

    - Track still alive  → EVADED
    - Track gone         → DESTROYED_LATE (may have been removed by other means)

    Returns list of newly finalized records (to broadcast as cop.bda events).
    """
    now = time.monotonic()
    finalized: List[Dict[str, Any]] = []
    for task_id in list(_PENDING_MISS):
        rec = _PENDING_MISS[task_id]
        if now < rec["_check_after"]:
            continue
        outcome = "EVADED" if rec["track_id"] in alive_track_ids else "DESTROYED_LATE"
        final_rec = {k: v for k, v in rec.items() if not k.startswith("_")}
        final_rec["outcome"]      = outcome
        final_rec["confirmed_at"] = datetime.now(timezone.utc).isoformat()
        _BDA_RECORDS.append(final_rec)
        finalized.append(final_rec)
        del _PENDING_MISS[task_id]
    return finalized


def get_all() -> List[Dict[str, Any]]:
    """All finalized BDA records, newest last."""
    return list(_BDA_RECORDS)


def get_pending() -> List[Dict[str, Any]]:
    """Pending miss records (outcome not yet confirmed), without internal keys."""
    return [
        {k: v for k, v in rec.items() if not k.startswith("_")}
        for rec in _PENDING_MISS.values()
    ]


def summary() -> Dict[str, Any]:
    """Aggregate stats — suitable for AAR inclusion."""
    all_recs = _BDA_RECORDS + get_pending()
    counts: Dict[str, int] = {}
    for rec in all_recs:
        counts[rec["outcome"]] = counts.get(rec["outcome"], 0) + 1
    total   = len(all_recs)
    destroyed = counts.get("DESTROYED", 0) + counts.get("DESTROYED_LATE", 0)
    return {
        "total_engagements": total,
        "destroyed":         destroyed,
        "evaded":            counts.get("EVADED", 0),
        "miss_pending":      counts.get("MISS", 0),
        "hit_rate_pct":      round(destroyed / max(1, total) * 100, 1),
        "by_outcome":        counts,
    }


def clear() -> None:
    """Reset state (called on server reset)."""
    _BDA_RECORDS.clear()
    _PENDING_MISS.clear()
