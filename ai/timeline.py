"""
ai/timeline.py  —  Per-track threat timeline history

Records time-series data for each track:
  - threat_score (0-100)
  - threat_level (LOW/MEDIUM/HIGH)
  - intent (unknown/loitering/reconnaissance/attack)
  - anomaly events (type + severity)

Used by frontend to render a threat timeline graph per track,
showing how a track's threat profile evolves over time.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

# ── Constants ───────────────────────────────────────────────────────────────

MAX_HISTORY_PER_TRACK = 200   # max data points per track
MAX_TRACKS            = 100   # max tracked timelines (LRU eviction)

# ── Storage ─────────────────────────────────────────────────────────────────

# {track_id: deque of {t, score, level, intent, events: [...]}}
_timelines: Dict[str, Deque[Dict[str, Any]]] = {}
_track_order: List[str] = []  # LRU order for eviction


def _touch(track_id: str) -> None:
    """Move track_id to end of LRU list."""
    if track_id in _track_order:
        _track_order.remove(track_id)
    _track_order.append(track_id)
    # Evict oldest if over limit
    while len(_track_order) > MAX_TRACKS:
        old = _track_order.pop(0)
        _timelines.pop(old, None)


def _ensure(track_id: str) -> Deque:
    if track_id not in _timelines:
        _timelines[track_id] = deque(maxlen=MAX_HISTORY_PER_TRACK)
    _touch(track_id)
    return _timelines[track_id]


# ── Recording API ──────────────────────────────────────────────────────────

def record_threat(
    track_id: str,
    score: int,
    level: str,
    intent: str,
    ts: Optional[float] = None,
) -> None:
    """Record a threat state snapshot for a track."""
    now = ts or time.time()
    history = _ensure(track_id)

    # Avoid duplicate entries within 0.5s
    if history and abs(history[-1]["t"] - now) < 0.5:
        # Update the last entry instead
        last = history[-1]
        last["score"] = score
        last["level"] = level
        last["intent"] = intent
        return

    history.append({
        "t": round(now, 2),
        "score": score,
        "level": level,
        "intent": intent,
        "events": [],
    })


def record_anomaly(
    track_id: str,
    anomaly_type: str,
    severity: str,
    ts: Optional[float] = None,
) -> None:
    """Attach an anomaly event to the latest timeline entry for a track."""
    now = ts or time.time()
    history = _ensure(track_id)

    event = {"type": anomaly_type, "severity": severity, "t": round(now, 2)}

    if history:
        # Attach to most recent entry
        history[-1]["events"].append(event)
    else:
        # No threat data yet — create a placeholder entry
        history.append({
            "t": round(now, 2),
            "score": 0,
            "level": "LOW",
            "intent": "unknown",
            "events": [event],
        })


# ── Query API ──────────────────────────────────────────────────────────────

def get_timeline(track_id: str) -> List[Dict[str, Any]]:
    """Get the full timeline history for a single track."""
    history = _timelines.get(track_id)
    if not history:
        return []
    return list(history)


def get_all_timelines() -> Dict[str, List[Dict[str, Any]]]:
    """Get timelines for all tracked tracks."""
    return {tid: list(h) for tid, h in _timelines.items()}


def get_active_track_ids() -> List[str]:
    """Get list of track IDs that have timeline data."""
    return list(_timelines.keys())


def get_summary() -> Dict[str, Any]:
    """Quick stats for AI status endpoint."""
    return {
        "tracked_count": len(_timelines),
        "total_points": sum(len(h) for h in _timelines.values()),
    }


# ── Lifecycle ──────────────────────────────────────────────────────────────

def remove_track(track_id: str) -> None:
    _timelines.pop(track_id, None)
    if track_id in _track_order:
        _track_order.remove(track_id)


def reset() -> None:
    _timelines.clear()
    _track_order.clear()
