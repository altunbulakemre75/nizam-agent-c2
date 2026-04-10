"""
ai/escalation.py — Unanswered ROE Advisory Escalation Engine

Tracks WEAPONS_FREE / WEAPONS_TIGHT advisories that have not been
acknowledged by an operator.  After configurable timeouts a graduated
escalation is emitted so the tactical layer can broadcast a
cop.escalation WebSocket event.

Escalation timeline (per track, per advisory cycle):
  t + 30s  → Level 1 WARNING   — reminder pulse
  t + 60s  → Level 2 CRITICAL  — urgent alarm

Acknowledgement:
  Call acknowledge(track_id, operator_id) when an operator acts
  (task approve/reject, explicit ACK via POST /api/roe/{tid}/ack).
  Also call resolve(track_id) when the track is destroyed / lost.

Usage (tactical background task):
    from ai import escalation as ai_esc

    escalations = ai_esc.check(roe_advisories)
    # escalations: List[Dict] — one entry per newly-triggered escalation

    ai_esc.acknowledge("T-001", "ops1")   # operator acked it
    ai_esc.resolve("T-001")               # track gone
    ai_esc.reset()                        # full state reset
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

# ── Configuration ──────────────────────────────────────────────────────────────

# Only escalate on these engagement levels
ESCALATION_ENGAGEMENTS = {"WEAPONS_FREE", "WEAPONS_TIGHT"}

# Time thresholds (seconds without operator acknowledgement)
ESCALATION_WARNING_S  = 30    # first reminder
ESCALATION_CRITICAL_S = 60    # urgent alarm

# ── Internal state ─────────────────────────────────────────────────────────────

# track_id → {first_seen_t, escalation_level, acknowledged, acknowledged_by,
#              acknowledged_at, engagement}
_pending: Dict[str, Dict[str, Any]] = {}


# ── Public API ─────────────────────────────────────────────────────────────────

def check(roe_advisories: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Evaluate current ROE advisories against the escalation clock.

    Should be called once per tactical cycle with the full list of active
    ROE advisories.

    Returns a list of newly-triggered escalation dicts:
        {
          "track_id":   str,
          "engagement": str,        # WEAPONS_FREE | WEAPONS_TIGHT
          "level":      str,        # "WARNING" | "CRITICAL"
          "duration_s": int,        # seconds since advisory first seen
          "message":    str,        # human-readable Turkish alert
        }
    """
    now = time.time()
    active_ids: set = set()
    triggered: List[Dict[str, Any]] = []

    for adv in roe_advisories:
        eng = adv.get("engagement", "")
        if eng not in ESCALATION_ENGAGEMENTS:
            continue
        track_id = adv.get("track_id", "")
        if not track_id:
            continue

        active_ids.add(track_id)

        state = _pending.get(track_id)
        if state is None:
            _pending[track_id] = {
                "first_seen_t":    now,
                "escalation_level": 0,
                "acknowledged":    False,
                "acknowledged_by": "",
                "acknowledged_at": None,
                "engagement":      eng,
            }
            state = _pending[track_id]
        else:
            # Update engagement level in case it upgraded this cycle
            state["engagement"] = eng

        # Already acknowledged — skip
        if state["acknowledged"]:
            continue

        duration = now - state["first_seen_t"]

        if duration >= ESCALATION_CRITICAL_S and state["escalation_level"] < 2:
            state["escalation_level"] = 2
            triggered.append(_make_event(track_id, eng, "CRITICAL", duration))

        elif duration >= ESCALATION_WARNING_S and state["escalation_level"] < 1:
            state["escalation_level"] = 1
            triggered.append(_make_event(track_id, eng, "WARNING", duration))

    # ── Prune tracks that are no longer in active advisories ──────────────────
    for track_id in list(_pending.keys()):
        if track_id not in active_ids:
            del _pending[track_id]

    return triggered


def acknowledge(track_id: str, operator_id: str = "") -> bool:
    """
    Mark an advisory as acknowledged by an operator.
    Returns True if the track was in the pending escalation pool.
    """
    state = _pending.get(track_id)
    if state:
        state["acknowledged"]    = True
        state["acknowledged_by"] = operator_id
        state["acknowledged_at"] = time.time()
        return True
    return False


def resolve(track_id: str) -> None:
    """Remove a track from the escalation pool (destroyed / lost)."""
    _pending.pop(track_id, None)


def get_pending() -> List[Dict[str, Any]]:
    """Return all currently pending (unacknowledged) escalation states."""
    now = time.time()
    result = []
    for track_id, state in _pending.items():
        if not state["acknowledged"]:
            result.append({
                "track_id":         track_id,
                "engagement":       state["engagement"],
                "duration_s":       round(now - state["first_seen_t"]),
                "escalation_level": state["escalation_level"],
            })
    return result


def reset() -> None:
    """Clear all escalation state (called on scenario reset)."""
    _pending.clear()


# ── Helpers ────────────────────────────────────────────────────────────────────

_LEVEL_MESSAGES = {
    "WARNING":  "{eng} {tid} {dur}s operator onayı bekleniyor — UYARI",
    "CRITICAL": "KRİTİK: {eng} {tid} {dur}s operator onayı alınamadı",
}

def _make_event(
    track_id: str,
    engagement: str,
    level: str,
    duration: float,
) -> Dict[str, Any]:
    dur = int(duration)
    msg = _LEVEL_MESSAGES[level].format(
        eng=engagement, tid=track_id, dur=dur,
    )
    return {
        "track_id":   track_id,
        "engagement": engagement,
        "level":      level,
        "duration_s": dur,
        "message":    msg,
    }
