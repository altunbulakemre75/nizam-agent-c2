"""
ai/deconfliction.py — Track deconfliction engine

Problem: Multiple sensor adapters (ADS-B, MQTT, REST, radar sim) may each
assign their own track ID to the same physical object. Without deconfliction
the COP state shows N duplicates of the same target — inflating threat counts
and confusing operator situational awareness.

Solution: Similarity scoring over position + kinematics. When a new track
arrives and scores above a confidence threshold against an existing track,
it is assigned the existing canonical ID. The duplicate ID is recorded as
an alias so the operator knows what was merged.

Similarity score (weighted sum, each component in [0, 1]):
  pos_score     = max(0, 1 - dist_m   / POS_GATE_M)     weight 0.55
  heading_score = max(0, 1 - hdiff    / HDG_GATE_DEG)   weight 0.25
  speed_score   = max(0, 1 - sdiff    / SPD_GATE_RATIO)  weight 0.20

Match threshold: MATCH_THRESHOLD = 0.65

Design notes:
  - False-positive merges (two separate threats merged into one) are more
    dangerous than missed merges (duplicate entries), so the threshold is
    intentionally conservative.
  - FRIENDLY tracks are never merged with HOSTILE/UNKNOWN tracks, even if
    they're in the same position (e.g. an interceptor reaching its target).
  - Thread-safe (lock around _aliases and _canonical_ids).
"""
from __future__ import annotations

import math
import threading
from typing import Dict, List, Optional, Tuple

# ── Constants ─────────────────────────────────────────────────────────────────

POS_GATE_M     = 200.0   # merge if within this distance
HDG_GATE_DEG   = 25.0    # merge if heading within this angle difference
SPD_GATE_RATIO = 0.35    # merge if speed within this ratio (0.35 = 35%)
MATCH_THRESHOLD = 0.65   # minimum weighted score to declare a match

# Weights must sum to 1.0
_W_POS = 0.55
_W_HDG = 0.25
_W_SPD = 0.20

DEG_TO_M = 111_320.0

# Friendly tracks are never merged with non-friendly tracks
_FRIENDLY_LABELS = {"friendly"}
_HOSTILE_LABELS  = {"drone", "helicopter", "fixed_wing", "unknown", "hostile"}


# ── State ─────────────────────────────────────────────────────────────────────

_lock = threading.Lock()

# alias_id → canonical_id  (e.g. "ADSB-001" → "T-R004-A012")
_aliases: Dict[str, str] = {}

# canonical_id → list of alias_ids that were merged into it
_merged_into: Dict[str, List[str]] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Fast flat-earth distance in metres."""
    dlat = (lat2 - lat1) * DEG_TO_M
    dlon = (lon2 - lon1) * DEG_TO_M * math.cos(math.radians((lat1 + lat2) / 2))
    return math.sqrt(dlat * dlat + dlon * dlon)


def _angle_diff(a: float, b: float) -> float:
    """Smallest absolute angle difference in [0, 180]."""
    d = abs(a - b) % 360
    return d if d <= 180 else 360 - d


def _similarity(new_t: dict, existing: dict) -> float:
    """
    Compute weighted similarity score in [0, 1] between two track payloads.
    Returns 0.0 immediately if position data is unavailable.
    """
    lat1 = new_t.get("lat")
    lon1 = new_t.get("lon")
    lat2 = existing.get("lat")
    lon2 = existing.get("lon")

    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return 0.0

    # 1) Position score
    dist = _dist_m(float(lat1), float(lon1), float(lat2), float(lon2))
    if dist > POS_GATE_M:
        return 0.0   # hard gate — never merge tracks > 200 m apart
    pos_score = max(0.0, 1.0 - dist / POS_GATE_M)

    # 2) Heading score
    h1 = new_t.get("heading") or (new_t.get("kinematics") or {}).get("heading_deg", 0.0)
    h2 = existing.get("heading") or (existing.get("kinematics") or {}).get("heading_deg", 0.0)
    hdiff = _angle_diff(float(h1 or 0), float(h2 or 0))
    hdg_score = max(0.0, 1.0 - hdiff / HDG_GATE_DEG)

    # 3) Speed score
    s1 = new_t.get("speed") or (new_t.get("kinematics") or {}).get("speed_mps", 0.0)
    s2 = existing.get("speed") or (existing.get("kinematics") or {}).get("speed_mps", 0.0)
    s1, s2 = float(s1 or 0), float(s2 or 0)
    if max(s1, s2) > 1.0:
        sratio = abs(s1 - s2) / max(s1, s2)
        spd_score = max(0.0, 1.0 - sratio / SPD_GATE_RATIO)
    else:
        spd_score = 1.0   # both stationary → speed not discriminating

    return _W_POS * pos_score + _W_HDG * hdg_score + _W_SPD * spd_score


def _is_friendly(track: dict) -> bool:
    label = (
        track.get("type")
        or (track.get("classification") or {}).get("label", "")
    )
    return str(label).lower() in _FRIENDLY_LABELS


# ── Public API ────────────────────────────────────────────────────────────────

def find_match(
    new_track: dict,
    existing_tracks: Dict[str, dict],
) -> Optional[Tuple[str, float]]:
    """
    Search existing_tracks for the best match to new_track.

    Returns (canonical_id, score) if a match above threshold is found,
    otherwise None.

    Thread-safe: may be called from multiple concurrent ingest tasks.
    """
    new_friendly = _is_friendly(new_track)
    best_id: Optional[str] = None
    best_score = MATCH_THRESHOLD  # must exceed threshold to match

    for tid, t in existing_tracks.items():
        # Never merge friendly ↔ hostile
        if _is_friendly(t) != new_friendly:
            continue
        score = _similarity(new_track, t)
        if score > best_score:
            best_score = score
            best_id = tid

    if best_id is None:
        return None
    return (best_id, round(best_score, 3))


def record_merge(alias_id: str, canonical_id: str) -> None:
    """
    Record that alias_id was merged into canonical_id.
    Idempotent — recording the same merge twice is safe.
    """
    with _lock:
        _aliases[alias_id] = canonical_id
        _merged_into.setdefault(canonical_id, [])
        if alias_id not in _merged_into[canonical_id]:
            _merged_into[canonical_id].append(alias_id)


def resolve(track_id: str) -> str:
    """
    Return the canonical ID for track_id (follows alias chain).
    Returns track_id itself if not an alias.
    """
    with _lock:
        return _aliases.get(track_id, track_id)


def get_aliases(canonical_id: str) -> List[str]:
    """Return all alias IDs merged into canonical_id."""
    with _lock:
        return list(_merged_into.get(canonical_id, []))


def merge_sensors(canonical: dict, duplicate: dict) -> list:
    """
    Merge supporting_sensors lists from two track payloads.
    Returns deduplicated list.
    """
    s1 = canonical.get("supporting_sensors") or []
    s2 = duplicate.get("supporting_sensors") or []
    seen = set(s1)
    merged = list(s1)
    for s in s2:
        if s not in seen:
            merged.append(s)
            seen.add(s)
    return merged


def stats() -> dict:
    """Return current deconfliction state summary."""
    with _lock:
        return {
            "total_aliases":   len(_aliases),
            "canonical_count": len(_merged_into),
            "alias_map":       dict(_aliases),
        }


def reset() -> None:
    """Clear all state (used between test runs and /api/reset)."""
    with _lock:
        _aliases.clear()
        _merged_into.clear()
