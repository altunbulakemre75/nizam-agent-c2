"""
ai/roe.py  —  Rules of Engagement (ROE) Advisory Engine

Evaluates each hostile/unknown track against engagement rules and
produces a clear engagement directive:

  WEAPONS_FREE  — Free to engage (track in kill zone, direct asset attack)
  WEAPONS_TIGHT — Engage only if hostile intent confirmed (HIGH + attack)
  WEAPONS_HOLD  — Engage only in self-defence (MEDIUM threat, uncertain)
  HOLD_FIRE     — Do not engage (LOW threat, friendly zone, unidentified)
  WARN          — Issue warning (approaching restricted zone)
  TRACK_ONLY    — Monitor, no engagement authorised

Factors considered:
  - Threat level & score
  - Intent classification
  - Zone context (kill/restricted/friendly)
  - Distance to nearest friendly asset
  - Coordinated attack membership
  - Closing velocity (approaching vs departing)
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Set, Tuple

# ── Constants ──────────────────────────────────────────────────────────────

DEG_TO_M = 111_320.0

# Engagement level hierarchy (strictest → most permissive)
ENGAGEMENT_LEVELS = [
    "HOLD_FIRE",     # 0 — no engagement
    "TRACK_ONLY",    # 1 — monitor
    "WARN",          # 2 — issue warning
    "WEAPONS_HOLD",  # 3 — self-defence only
    "WEAPONS_TIGHT", # 4 — engage if hostile intent
    "WEAPONS_FREE",  # 5 — free to engage
]
_LEVEL_INDEX = {v: i for i, v in enumerate(ENGAGEMENT_LEVELS)}

# Distance thresholds
CLOSE_RANGE_M    = 500.0    # very close — potential immediate threat
MEDIUM_RANGE_M   = 1500.0   # medium proximity
LONG_RANGE_M     = 3000.0   # beyond engagement range

# Cooldown for advisory changes per track
COOLDOWN_S = 10.0

# ── Helpers ────────────────────────────────────────────────────────────────

def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = (lat2 - lat1) * DEG_TO_M
    dlon = (lon2 - lon1) * DEG_TO_M * math.cos(math.radians((lat1 + lat2) / 2))
    return math.sqrt(dlat * dlat + dlon * dlon)


def _point_in_polygon(lat: float, lon: float, coords: List) -> bool:
    n = len(coords)
    if n < 3:
        return False
    inside = False
    x, y = lon, lat
    j = n - 1
    for i in range(n):
        xi, yi = coords[i][1], coords[i][0]
        xj, yj = coords[j][1], coords[j][0]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-15) + xi):
            inside = not inside
        j = i
    return inside


def _nearest_polygon_dist(lat: float, lon: float, coords: List) -> float:
    if _point_in_polygon(lat, lon, coords):
        return 0.0
    min_d = float("inf")
    for c in coords:
        d = _dist_m(lat, lon, c[0], c[1])
        if d < min_d:
            min_d = d
    return min_d


# ── ROE Evaluation ─────────────────────────────────────────────────────────

_cooldowns: Dict[str, float] = {}
_last_advisories: Dict[str, Dict] = {}


def evaluate_track(
    track_id: str,
    track: Dict[str, Any],
    threat: Optional[Dict[str, Any]],
    zones: Dict[str, Dict],
    assets: Dict[str, Dict],
    coord_attack_ids: Set[str],
) -> Optional[Dict[str, Any]]:
    """
    Evaluate ROE for a single track.
    Returns an advisory dict or None if nothing changed.
    """
    lat = track.get("lat")
    lon = track.get("lon")
    if lat is None or lon is None:
        return None

    level = (threat or {}).get("threat_level", track.get("threat_level", "LOW"))
    score = int((threat or {}).get("score", (threat or {}).get("threat_score", 0)))
    intent = track.get("intent", (threat or {}).get("intent", "unknown"))
    speed = track.get("speed") or track.get("kinematics", {}).get("speed_mps") or 0

    # ── Zone context ──
    in_kill_zone = False
    in_restricted_zone = False
    in_friendly_zone = False
    zone_name = ""
    zone_dist_restricted = float("inf")

    for zid, zone in zones.items():
        coords = zone.get("coordinates", [])
        if not coords or len(coords) < 3:
            continue
        ztype = zone.get("type", "restricted")
        if _point_in_polygon(lat, lon, coords):
            if ztype == "kill":
                in_kill_zone = True
                zone_name = zone.get("name", zid)
            elif ztype == "restricted":
                in_restricted_zone = True
                zone_name = zone.get("name", zid)
            elif ztype == "friendly":
                in_friendly_zone = True
                zone_name = zone.get("name", zid)
        else:
            if ztype == "restricted":
                d = _nearest_polygon_dist(lat, lon, coords)
                if d < zone_dist_restricted:
                    zone_dist_restricted = d

    # ── Distance to nearest friendly asset ──
    friendlies = {k: v for k, v in assets.items()
                  if v.get("type") == "friendly" and v.get("status") == "active"}
    min_asset_dist = float("inf")
    nearest_asset_name = ""
    for aid, asset in friendlies.items():
        alat, alon = asset.get("lat"), asset.get("lon")
        if alat is None or alon is None:
            continue
        d = _dist_m(lat, lon, alat, alon)
        if d < min_asset_dist:
            min_asset_dist = d
            nearest_asset_name = asset.get("name", aid)

    # ── Coordinated attack membership ──
    is_coordinated = track_id in coord_attack_ids

    # ── ROE Decision Matrix ──
    engagement = "TRACK_ONLY"
    reasons: List[str] = []
    urgency = "LOW"  # LOW / MEDIUM / HIGH / CRITICAL

    # Rule 1: Track in kill zone → WEAPONS_FREE
    if in_kill_zone and level in ("HIGH", "MEDIUM"):
        engagement = "WEAPONS_FREE"
        reasons.append(f"Kill zone '{zone_name}' icinde")
        urgency = "CRITICAL"

    # Rule 2: HIGH threat + attack intent + close to asset → WEAPONS_FREE
    elif (level == "HIGH" and intent == "attack" and
          min_asset_dist < CLOSE_RANGE_M):
        engagement = "WEAPONS_FREE"
        reasons.append(f"Saldiri niyetli HIGH tehdit, {nearest_asset_name}'a {round(min_asset_dist)}m")
        urgency = "CRITICAL"

    # Rule 3: HIGH threat + attack intent → WEAPONS_TIGHT
    elif level == "HIGH" and intent == "attack":
        engagement = "WEAPONS_TIGHT"
        reasons.append("HIGH tehdit, saldiri niyeti onaylandi")
        urgency = "HIGH"
        if is_coordinated:
            engagement = "WEAPONS_FREE"
            reasons.append("Koordineli saldiri uyesi")
            urgency = "CRITICAL"

    # Rule 4: HIGH threat + unknown/recon intent + close → WEAPONS_TIGHT
    elif level == "HIGH" and min_asset_dist < MEDIUM_RANGE_M:
        engagement = "WEAPONS_TIGHT"
        reasons.append(f"HIGH tehdit, {nearest_asset_name}'a {round(min_asset_dist)}m yakinlikta")
        urgency = "HIGH"

    # Rule 5: HIGH threat + far → WEAPONS_HOLD
    elif level == "HIGH":
        engagement = "WEAPONS_HOLD"
        reasons.append("HIGH tehdit, menzil disinda")
        urgency = "MEDIUM"

    # Rule 6: MEDIUM threat + approaching restricted zone → WARN
    elif level == "MEDIUM" and zone_dist_restricted < MEDIUM_RANGE_M:
        engagement = "WARN"
        reasons.append(f"MEDIUM tehdit, kisitli bolgeye {round(zone_dist_restricted)}m")
        urgency = "MEDIUM"

    # Rule 7: MEDIUM threat + in restricted zone → WEAPONS_HOLD
    elif level == "MEDIUM" and in_restricted_zone:
        engagement = "WEAPONS_HOLD"
        reasons.append(f"MEDIUM tehdit, '{zone_name}' kisitli bolge icinde")
        urgency = "MEDIUM"

    # Rule 8: MEDIUM threat + coordinated attack → WEAPONS_TIGHT
    elif level == "MEDIUM" and is_coordinated:
        engagement = "WEAPONS_TIGHT"
        reasons.append("MEDIUM tehdit, koordineli saldiri uyesi")
        urgency = "HIGH"

    # Rule 9: MEDIUM threat general → TRACK_ONLY
    elif level == "MEDIUM":
        engagement = "TRACK_ONLY"
        reasons.append("MEDIUM tehdit, izlemeye devam")
        urgency = "LOW"

    # Rule 10: In friendly zone → HOLD_FIRE
    elif in_friendly_zone:
        engagement = "HOLD_FIRE"
        reasons.append(f"Dost bolge '{zone_name}' icinde")
        urgency = "LOW"

    # Rule 11: LOW threat → TRACK_ONLY
    elif level == "LOW":
        engagement = "TRACK_ONLY"
        reasons.append("Dusuk tehdit")
        urgency = "LOW"

    # Additional modifiers
    if is_coordinated and engagement not in ("WEAPONS_FREE",):
        old_idx = _LEVEL_INDEX.get(engagement, 0)
        new_idx = min(old_idx + 1, len(ENGAGEMENT_LEVELS) - 1)
        if new_idx > old_idx:
            engagement = ENGAGEMENT_LEVELS[new_idx]
            reasons.append("Koordineli saldiri uyesi (+1 seviye)")
        if urgency == "LOW":
            urgency = "MEDIUM"

    if score >= 90 and engagement in ("WEAPONS_HOLD", "TRACK_ONLY"):
        engagement = "WEAPONS_TIGHT"
        reasons.append(f"Skor cok yuksek ({score})")
        urgency = "HIGH"

    advisory = {
        "track_id": track_id,
        "engagement": engagement,
        "engagement_level": _LEVEL_INDEX.get(engagement, 0),
        "urgency": urgency,
        "reasons": reasons,
        "threat_level": level,
        "threat_score": score,
        "intent": intent,
        "in_kill_zone": in_kill_zone,
        "in_restricted_zone": in_restricted_zone,
        "in_friendly_zone": in_friendly_zone,
        "is_coordinated": is_coordinated,
        "nearest_asset": nearest_asset_name,
        "nearest_asset_dist_m": round(min_asset_dist) if min_asset_dist < float("inf") else None,
        "message": _build_message(track_id, engagement, urgency, reasons),
        "time": time.time(),
    }

    return advisory


def _build_message(
    track_id: str,
    engagement: str,
    urgency: str,
    reasons: List[str],
) -> str:
    """Build human-readable ROE advisory message."""
    reason_str = "; ".join(reasons)
    return f"[{engagement}] {track_id} — {reason_str}"


# ── Batch Evaluation ──────────────────────────────────────────────────────

def evaluate_all(
    tracks: Dict[str, Dict],
    threats: Dict[str, Dict],
    zones: Dict[str, Dict],
    assets: Dict[str, Dict],
    coord_attacks: List[Dict],
) -> List[Dict[str, Any]]:
    """
    Evaluate ROE for all tracks and return advisory list.
    Sorted by engagement level (most permissive first = most urgent).
    """
    # Build set of track IDs involved in coordinated attacks
    coord_ids: Set[str] = set()
    for ca in coord_attacks:
        for tid in ca.get("track_ids", []):
            coord_ids.add(tid)

    advisories: List[Dict[str, Any]] = []

    for tid, track in tracks.items():
        threat = threats.get(tid)
        # Only evaluate tracks that have some threat assessment
        level = (threat or {}).get("threat_level", track.get("threat_level"))
        if not level:
            continue

        adv = evaluate_track(tid, track, threat, zones, assets, coord_ids)
        if adv and adv["engagement"] != "TRACK_ONLY":
            advisories.append(adv)

    # Sort: highest engagement level first, then by urgency
    urgency_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    advisories.sort(key=lambda a: (
        -a["engagement_level"],
        urgency_order.get(a["urgency"], 9),
    ))

    return advisories


# ── Lifecycle ──────────────────────────────────────────────────────────────

def reset() -> None:
    _cooldowns.clear()
    _last_advisories.clear()
