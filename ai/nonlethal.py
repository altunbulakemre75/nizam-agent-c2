"""
ai/nonlethal.py — Non-lethal Engagement Recommendation Engine

Identifies non-lethal capable assets (jammers, EW systems, GPS spoofers) and
recommends JAM, SPOOF, or EW_SUPPRESS tasks as lower-escalation alternatives
to kinetic ENGAGE.

Non-lethal action mapping (asset type/capability keyword → task action):
  jammer     → JAM          (RF uplink disruption)
  spoofer    → SPOOF        (GPS navigation corruption)
  gps_spoof  → SPOOF
  ew         → EW_SUPPRESS  (broadband electronic suppression)
  soft_kill  → EW_SUPPRESS
  electronic → EW_SUPPRESS

Scoring rules:
  score >= KINETIC_ONLY_SCORE (90) → imminent; skip to ENGAGE only
  score 60–89 + NL effector in range → recommend NL task alongside ENGAGE
  score < 60                         → OBSERVE only (handled upstream)

Usage:
    from ai.nonlethal import recommend, NON_LETHAL_ACTIONS

    nl_tasks = recommend(threat_id, threat, assets)
    # nl_tasks: [{"action":"JAM", "effector_id":..., "effector_name":..., "dist_km":...}, ...]
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

DEG_TO_M = 111_320.0

# Capability keyword → task action (first match wins per asset)
NON_LETHAL_ACTIONS: Dict[str, str] = {
    "jammer":     "JAM",
    "spoofer":    "SPOOF",
    "gps_spoof":  "SPOOF",
    "ew":         "EW_SUPPRESS",
    "soft_kill":  "EW_SUPPRESS",
    "electronic": "EW_SUPPRESS",
}

# Score thresholds
MIN_SCORE_NL       = 60   # below this → OBSERVE only (no NL recommendation)
KINETIC_ONLY_SCORE = 90   # at/above this → skip NL (too imminent, go kinetic)

# Default effective range if not specified on the asset
DEFAULT_NL_RANGE_KM = 3.0


def _dist_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = (lat2 - lat1) * DEG_TO_M
    dlon = (lon2 - lon1) * DEG_TO_M * math.cos(math.radians((lat1 + lat2) / 2))
    return math.sqrt(dlat * dlat + dlon * dlon) / 1000.0


def _nl_action(asset: Dict[str, Any]) -> Optional[str]:
    """Return the non-lethal action name for this asset, or None if not NL capable."""
    combined = (
        (asset.get("type") or "") + " " + (asset.get("capability") or "")
    ).lower()
    for kw, action in NON_LETHAL_ACTIONS.items():
        if kw in combined:
            return action
    return None


def recommend(
    threat_id: str,       # reserved for future per-track suppression dedup  # noqa: ARG001
    threat: Dict[str, Any],
    assets: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Return non-lethal task option dicts for a given threat.

    Each returned dict has keys:
        action        — "JAM" | "SPOOF" | "EW_SUPPRESS"
        effector_id   — asset ID of the capable effector
        effector_name — human-readable name
        dist_km       — distance from effector to threat (km)

    Returns [] when:
      • threat score is outside the [MIN_SCORE_NL, KINETIC_ONLY_SCORE) window
      • threat level is not HIGH or MEDIUM
      • threat intent is not "attack" or "unknown"
      • no NL-capable effectors are active and within range
    """
    score  = int(threat.get("score") or threat.get("threat_score") or 0)
    level  = threat.get("threat_level", "LOW")
    intent = threat.get("intent", "unknown")

    if score >= KINETIC_ONLY_SCORE:
        return []
    if score < MIN_SCORE_NL:
        return []
    if level not in ("HIGH", "MEDIUM"):
        return []
    if intent not in ("attack", "unknown"):
        return []

    tlat = float(threat.get("lat") or 0.0)
    tlon = float(threat.get("lon") or 0.0)

    # One task per action type — use the closest effector for each
    best: Dict[str, Dict[str, Any]] = {}   # action → best candidate so far

    for aid, asset in assets.items():
        if asset.get("status", "active") != "active":
            continue
        action = _nl_action(asset)
        if not action:
            continue
        alat = float(asset.get("lat") or 0.0)
        alon = float(asset.get("lon") or 0.0)
        if alat == 0.0 and alon == 0.0:
            continue
        rng = float(
            asset.get("range_km") or asset.get("range") or DEFAULT_NL_RANGE_KM
        )
        dist = _dist_km(tlat, tlon, alat, alon)
        if dist > rng:
            continue

        # Keep the closest effector per action type
        prev = best.get(action)
        if prev is None or dist < prev["dist_km"]:
            best[action] = {
                "action":        action,
                "effector_id":   aid,
                "effector_name": asset.get("name") or aid,
                "dist_km":       round(dist, 2),
            }

    return list(best.values())
