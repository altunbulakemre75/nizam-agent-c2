"""
ai/lineage.py  —  Decision lineage store for NIZAM

Records the chain of decisions that led to a track's current state.
Answers the question: "why is this track classified as HIGH threat?"

Every AI subsystem (ml_threat, tactical, roe, task_proposer, fire_control)
appends a LineageRecord to the track's chain when it makes a decision.
The COP server exposes the chain via GET /api/lineage/{track_id} and the
browser UI renders it as a timeline when the operator right-clicks a track.

Design:
  - Thread-safe (called from both sync AI code and the async server task)
  - Bounded: ring buffer per track + eviction of oldest tracks at capacity
  - Zero external deps, O(1) append, O(n) read per track
"""

from __future__ import annotations

import threading
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_MAX_RECORDS_PER_TRACK = 50      # keep last N decisions per track
_MAX_TRACKS = 500                 # evict oldest track when exceeded


# ---------------------------------------------------------------------------
# Store (module-level singleton — intentional, matches ai/ pattern)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_store: Dict[str, Deque[Dict[str, Any]]] = defaultdict(
    lambda: deque(maxlen=_MAX_RECORDS_PER_TRACK)
)
_track_order: Deque[str] = deque()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def record(
    track_id: str,
    stage: str,
    summary: str,
    inputs: Optional[Dict[str, Any]] = None,
    outputs: Optional[Dict[str, Any]] = None,
    rule: Optional[str] = None,
) -> None:
    """
    Record a single decision step in a track's lineage.

    Parameters
    ----------
    track_id : str
        The track this decision is about (e.g. "T-R012-A018").
    stage : str
        Subsystem that produced it. Known stages:
        fuser, ml_threat, tactical, anomaly, coord_attack, roe,
        task_proposer, fire_control.
    summary : str
        One-line human-readable description for the UI timeline.
    inputs : dict, optional
        What went in: feature values, sensor IDs, prior state.
    outputs : dict, optional
        What came out: score, classification, recommendation.
    rule : str, optional
        Which rule / model / heuristic triggered this decision.
    """
    if not track_id:
        return

    record_obj = {
        "decision_id": uuid.uuid4().hex[:12],
        "timestamp": _utc_now_iso(),
        "stage": stage,
        "summary": summary,
        "inputs": inputs or {},
        "outputs": outputs or {},
        "rule": rule,
    }

    with _lock:
        is_new = track_id not in _store
        _store[track_id].append(record_obj)
        if is_new:
            _track_order.append(track_id)
            while len(_track_order) > _MAX_TRACKS:
                oldest = _track_order.popleft()
                _store.pop(oldest, None)


def get_chain(track_id: str) -> List[Dict[str, Any]]:
    """Return all lineage records for a track, oldest first."""
    with _lock:
        return list(_store.get(track_id, []))


def get_summary(track_id: str) -> Dict[str, Any]:
    """Short summary: total records, stages involved, time range."""
    with _lock:
        chain = list(_store.get(track_id, []))

    if not chain:
        return {
            "track_id": track_id,
            "count": 0,
            "stages": [],
            "first": None,
            "last": None,
        }
    stages = sorted({r["stage"] for r in chain})
    return {
        "track_id": track_id,
        "count": len(chain),
        "stages": stages,
        "first": chain[0]["timestamp"],
        "last": chain[-1]["timestamp"],
    }


def get_all_track_ids() -> List[str]:
    """List every track that has lineage records."""
    with _lock:
        return list(_store.keys())


def drop_track(track_id: str) -> None:
    """Remove all lineage for a track (called when track is engaged/killed)."""
    with _lock:
        _store.pop(track_id, None)
        try:
            _track_order.remove(track_id)
        except ValueError:
            pass


def clear() -> None:
    """Wipe the entire lineage store (used by /api/reset and tests)."""
    with _lock:
        _store.clear()
        _track_order.clear()


def stats() -> Dict[str, Any]:
    with _lock:
        return {
            "tracks": len(_store),
            "total_records": sum(len(q) for q in _store.values()),
            "max_per_track": _MAX_RECORDS_PER_TRACK,
            "max_tracks": _MAX_TRACKS,
        }
