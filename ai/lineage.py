"""
ai/lineage.py  —  Cryptographically-linked decision lineage for NIZAM

Records the chain of decisions that led to a track's current state.
Answers the question: "why is this track classified as HIGH threat?"

Every AI subsystem (ml_threat, tactical, roe, task_proposer, fire_control)
appends a LineageRecord to the track's chain when it makes a decision.
The COP server exposes the chain via GET /api/lineage/{track_id} and the
browser UI renders it as a timeline when the operator right-clicks a track.

Each record carries a SHA-256 hash of its content and a `prev_hash` pointer
to the preceding record.  This makes the chain tamper-evident: modifying or
deleting any record breaks the hash chain, detectable by `verify_chain()`.

Design:
  - Thread-safe (called from both sync AI code and the async server task)
  - Bounded: ring buffer per track + eviction of oldest tracks at capacity
  - SHA-256 hash chain per track — append-only, tamper-evident
  - Zero external deps, O(1) append, O(n) read/verify per track
"""

from __future__ import annotations

import hashlib
import json
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


def _hash_record(record_obj: Dict[str, Any]) -> str:
    """Deterministic SHA-256 hash of a record's content fields."""
    canonical = json.dumps(
        {k: v for k, v in record_obj.items() if k not in ("hash", "prev_hash")},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


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

    Each record is SHA-256 hashed and linked to the previous record's hash,
    forming a tamper-evident chain per track.

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
        chain = _store[track_id]

        # Link to previous record's hash (genesis record has prev_hash "0"*64)
        if chain:
            record_obj["prev_hash"] = chain[-1].get("hash", "0" * 64)
        else:
            record_obj["prev_hash"] = "0" * 64

        record_obj["hash"] = _hash_record(record_obj)

        chain.append(record_obj)
        if is_new:
            _track_order.append(track_id)
            while len(_track_order) > _MAX_TRACKS:
                oldest = _track_order.popleft()
                _store.pop(oldest, None)


def record_batch(records: List[Dict[str, Any]]) -> None:
    """
    Append multiple lineage records in a single lock acquisition.

    Each element must be a dict with keys: track_id, stage, summary,
    and optionally inputs, outputs, rule.  Much faster than calling
    record() in a loop when there are many records per tactical pass.
    """
    if not records:
        return

    with _lock:
        for rec_input in records:
            track_id = rec_input.get("track_id")
            if not track_id:
                continue

            record_obj = {
                "decision_id": uuid.uuid4().hex[:12],
                "timestamp": _utc_now_iso(),
                "stage": rec_input.get("stage", ""),
                "summary": rec_input.get("summary", ""),
                "inputs": rec_input.get("inputs") or {},
                "outputs": rec_input.get("outputs") or {},
                "rule": rec_input.get("rule"),
            }

            is_new = track_id not in _store
            chain = _store[track_id]

            if chain:
                record_obj["prev_hash"] = chain[-1].get("hash", "0" * 64)
            else:
                record_obj["prev_hash"] = "0" * 64

            record_obj["hash"] = _hash_record(record_obj)
            chain.append(record_obj)

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


def verify_chain(track_id: str) -> Dict[str, Any]:
    """
    Verify the hash chain integrity for a track.

    Returns {"valid": True/False, "records": N, "broken_at": index or None}.
    """
    with _lock:
        chain = list(_store.get(track_id, []))

    if not chain:
        return {"valid": True, "records": 0, "broken_at": None}

    for i, rec in enumerate(chain):
        # Verify own hash
        expected = _hash_record(rec)
        if rec.get("hash") != expected:
            return {"valid": False, "records": len(chain), "broken_at": i,
                    "reason": "hash mismatch"}

        # Verify prev_hash linkage
        if i == 0:
            if rec.get("prev_hash") != "0" * 64:
                return {"valid": False, "records": len(chain), "broken_at": 0,
                        "reason": "genesis prev_hash invalid"}
        else:
            if rec.get("prev_hash") != chain[i - 1].get("hash"):
                return {"valid": False, "records": len(chain), "broken_at": i,
                        "reason": "prev_hash linkage broken"}

    return {"valid": True, "records": len(chain), "broken_at": None}


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
