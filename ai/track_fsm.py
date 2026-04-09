"""
ai/track_fsm.py — Track lifecycle state machine

States:
  DETECTED  — first sensor contact, unconfirmed
  TRACKED   — confirmed by 2+ sensors or N updates
  ENGAGING  — approved ENGAGE task in progress
  DESTROYED — effector impact confirmed

Transitions:
  DETECTED  → TRACKED    (2+ sensors or 3+ updates)
  TRACKED   → ENGAGING   (operator approves ENGAGE task)
  ENGAGING  → DESTROYED  (effector impact confirmed)
  any       → LOST       (no update for TIMEOUT_S seconds)

Thread-safe, O(1) per operation.
"""
from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Dict, Optional


class TrackState(str, Enum):
    DETECTED  = "DETECTED"
    TRACKED   = "TRACKED"
    ENGAGING  = "ENGAGING"
    DESTROYED = "DESTROYED"
    LOST      = "LOST"


# How many updates to auto-promote DETECTED → TRACKED (if <2 sensors)
_PROMOTE_AFTER_UPDATES = 3
# Seconds without update before a track is considered LOST
_LOST_TIMEOUT_S = 60.0


class _TrackEntry:
    __slots__ = ("state", "updates", "last_seen", "sensors")

    def __init__(self):
        self.state: TrackState = TrackState.DETECTED
        self.updates: int = 0
        self.last_seen: float = time.time()
        self.sensors: set = set()


_lock = threading.Lock()
_tracks: Dict[str, _TrackEntry] = {}


# ── Public API ────────────────────────────────────────────────────────────────

def on_update(track_id: str, sensors: Optional[list] = None) -> TrackState:
    """Called on every track update. Returns current state after transition."""
    with _lock:
        entry = _tracks.get(track_id)
        if entry is None:
            entry = _TrackEntry()
            _tracks[track_id] = entry

        if entry.state in (TrackState.DESTROYED, TrackState.LOST):
            return entry.state

        entry.updates += 1
        entry.last_seen = time.time()
        if sensors:
            entry.sensors.update(sensors)

        # Auto-promote DETECTED → TRACKED
        if entry.state == TrackState.DETECTED:
            if len(entry.sensors) >= 2 or entry.updates >= _PROMOTE_AFTER_UPDATES:
                entry.state = TrackState.TRACKED

        return entry.state


def on_engage(track_id: str) -> TrackState:
    """Called when operator approves ENGAGE task."""
    with _lock:
        entry = _tracks.get(track_id)
        if entry is None:
            return TrackState.DETECTED
        if entry.state == TrackState.TRACKED:
            entry.state = TrackState.ENGAGING
        return entry.state


def on_destroyed(track_id: str) -> TrackState:
    """Called after effector impact confirmed."""
    with _lock:
        entry = _tracks.get(track_id)
        if entry is None:
            return TrackState.DESTROYED
        entry.state = TrackState.DESTROYED
        return entry.state


def get_state(track_id: str) -> Optional[TrackState]:
    """Return current state or None if unknown."""
    with _lock:
        entry = _tracks.get(track_id)
        if entry is None:
            return None
        # Check for timeout → LOST
        if entry.state not in (TrackState.DESTROYED, TrackState.LOST):
            if time.time() - entry.last_seen > _LOST_TIMEOUT_S:
                entry.state = TrackState.LOST
        return entry.state


def get_all() -> Dict[str, str]:
    """Return {track_id: state} for all tracks."""
    now = time.time()
    with _lock:
        result = {}
        for tid, entry in _tracks.items():
            if entry.state not in (TrackState.DESTROYED, TrackState.LOST):
                if now - entry.last_seen > _LOST_TIMEOUT_S:
                    entry.state = TrackState.LOST
            result[tid] = entry.state.value
        return result


def drop_track(track_id: str) -> None:
    with _lock:
        _tracks.pop(track_id, None)


def clear() -> None:
    with _lock:
        _tracks.clear()


def stats() -> Dict:
    with _lock:
        counts = {}
        for entry in _tracks.values():
            counts[entry.state.value] = counts.get(entry.state.value, 0) + 1
        return {"total": len(_tracks), "by_state": counts}
