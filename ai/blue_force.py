"""
ai/blue_force.py — Blue Force / Fratricide Prevention

Before authorising a WEAPONS_FREE engagement, checks whether any
friendly asset lies within the projected engagement corridor.

Engagement corridor definition:
  A cylinder of radius LETHAL_RADIUS_M centred on the straight-line
  path from the effector (or the nearest friendly asset used as proxy)
  to the threat track's current position.

  For each point p on the line from effector E to target T, we check
  whether any friendly asset F satisfies dist(F, p) < LETHAL_RADIUS_M.
  This is equivalent to computing the perpendicular distance from F to
  segment ET and comparing to the threshold.

If a fratricide risk is detected:
  - Returns a BFT_RISK warning dict
  - The ROE engine should downgrade WEAPONS_FREE → WEAPONS_HOLD
    (handled by check_advisories())

Usage:
    from ai.blue_force import check_advisories, BFT_LETHAL_RADIUS_M

    safe_advisories, warnings = check_advisories(
        advisories, tracks, assets
    )
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

# ── Constants ──────────────────────────────────────────────────────────────────

DEG_TO_M = 111_320.0

# Lateral clearance required between engagement path and friendly asset
BFT_LETHAL_RADIUS_M: float = 200.0   # metres — operator-configurable

# Maximum engagement range considered (beyond this a threat is not assignable)
MAX_ENGAGEMENT_RANGE_M: float = 5000.0


# ── Geometry helpers ───────────────────────────────────────────────────────────

def _to_xy(lat: float, lon: float, ref_lat: float) -> Tuple[float, float]:
    """Convert lat/lon to local Cartesian (metres) relative to a reference lat."""
    x = lon * DEG_TO_M * math.cos(math.radians(ref_lat))
    y = lat * DEG_TO_M
    return x, y


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = (lat2 - lat1) * DEG_TO_M
    dlon = (lon2 - lon1) * DEG_TO_M * math.cos(math.radians((lat1 + lat2) / 2))
    return math.sqrt(dlat * dlat + dlon * dlon)


def _point_to_segment_dist(
    px: float, py: float,
    ax: float, ay: float,
    bx: float, by: float,
) -> float:
    """
    Return the perpendicular distance from point P to line segment AB.
    If the foot of the perpendicular lies outside AB, returns the distance
    to the nearer endpoint.
    """
    abx, aby = bx - ax, by - ay
    len_sq = abx * abx + aby * aby
    if len_sq == 0:
        return math.sqrt((px - ax) ** 2 + (py - ay) ** 2)
    t = ((px - ax) * abx + (py - ay) * aby) / len_sq
    t = max(0.0, min(1.0, t))
    nearest_x = ax + t * abx
    nearest_y = ay + t * aby
    return math.sqrt((px - nearest_x) ** 2 + (py - nearest_y) ** 2)


# ── Core check ─────────────────────────────────────────────────────────────────

def _friendly_in_corridor(
    threat_lat: float, threat_lon: float,
    effector_lat: float, effector_lon: float,
    friendlies: Dict[str, Dict[str, Any]],
    lethal_radius_m: float = BFT_LETHAL_RADIUS_M,
) -> List[Dict[str, Any]]:
    """
    Return list of friendly assets that fall within the engagement corridor
    (segment from effector to threat).
    """
    ref_lat = (threat_lat + effector_lat) / 2
    tx, ty  = _to_xy(threat_lat,   threat_lon,   ref_lat)
    ex, ey  = _to_xy(effector_lat, effector_lon, ref_lat)

    at_risk = []
    for fid, fa in friendlies.items():
        flat = fa.get("lat")
        flon = fa.get("lon")
        if flat is None or flon is None:
            continue
        fx, fy = _to_xy(flat, flon, ref_lat)
        d = _point_to_segment_dist(fx, fy, ex, ey, tx, ty)
        if d < lethal_radius_m:
            at_risk.append({
                "asset_id":   fid,
                "asset_name": fa.get("name", fid),
                "clearance_m": round(d, 1),
            })
    return at_risk


def check_advisories(
    advisories: List[Dict[str, Any]],
    tracks:     Dict[str, Dict[str, Any]],
    assets:     Dict[str, Dict[str, Any]],
    lethal_radius_m: float = BFT_LETHAL_RADIUS_M,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Screen ROE advisories for fratricide risk.

    For each WEAPONS_FREE advisory:
      1. Find the nearest friendly asset to act as proxy effector origin.
      2. Check whether any other friendly asset lies inside the engagement
         corridor (effector → target).
      3. If risk detected: downgrade advisory to WEAPONS_HOLD, add
         BFT_WARNING reason, return a warning record.

    Parameters
    ----------
    advisories       : current ROE advisory list from ai/roe.py
    tracks           : current track dict from STATE["tracks"]
    assets           : current asset dict from STATE["assets"]
    lethal_radius_m  : corridor half-width in metres

    Returns
    -------
    (safe_advisories, bft_warnings)
      safe_advisories : updated advisory list (WEAPONS_FREE may be downgraded)
      bft_warnings    : list of warning dicts (one per affected track)
    """
    from ai.roe import ENGAGEMENT_LEVELS, _LEVEL_INDEX

    friendlies = {
        k: v for k, v in assets.items()
        if v.get("type") == "friendly" and v.get("status", "active") == "active"
        and v.get("lat") is not None
    }

    safe_advisories: List[Dict[str, Any]] = []
    bft_warnings:    List[Dict[str, Any]] = []

    for adv in advisories:
        if adv.get("engagement") != "WEAPONS_FREE":
            safe_advisories.append(adv)
            continue

        tid   = adv.get("track_id", "")
        track = tracks.get(tid, {})
        tlat  = track.get("lat")
        tlon  = track.get("lon")

        if tlat is None or tlon is None:
            safe_advisories.append(adv)
            continue

        # Check if at least one friendly is within engagement range
        reachable = [
            (fid, fa) for fid, fa in friendlies.items()
            if _dist_m(tlat, tlon, fa["lat"], fa["lon"]) <= MAX_ENGAGEMENT_RANGE_M
        ]
        if not reachable:
            # No effector reachable — engagement infeasible, downgrade
            adv = dict(adv)
            adv["engagement"]       = "WEAPONS_HOLD"
            adv["engagement_level"] = _LEVEL_INDEX.get("WEAPONS_HOLD", 3)
            adv["reasons"]          = list(adv.get("reasons", [])) + [
                "Menzil içinde effektör yok — angajman yapılamaz"
            ]
            safe_advisories.append(adv)
            continue

        # For each possible effector (every friendly in range), check if any
        # OTHER friendly lies inside the engagement corridor.  Use worst-case:
        # collect all at-risk assets across all potential firing solutions.
        at_risk_map: Dict[str, Dict] = {}
        for eff_id, eff_asset in reachable:
            others = {k: v for k, v in friendlies.items() if k != eff_id}
            for r in _friendly_in_corridor(
                tlat, tlon,
                eff_asset["lat"], eff_asset["lon"],
                others,
                lethal_radius_m=lethal_radius_m,
            ):
                # Keep the minimum clearance observed per asset
                existing = at_risk_map.get(r["asset_id"])
                if existing is None or r["clearance_m"] < existing["clearance_m"]:
                    at_risk_map[r["asset_id"]] = r
        at_risk = list(at_risk_map.values())

        if at_risk:
            adv = dict(adv)
            asset_names = ", ".join(r["asset_name"] for r in at_risk)
            adv["engagement"]       = "WEAPONS_HOLD"
            adv["engagement_level"] = _LEVEL_INDEX.get("WEAPONS_HOLD", 3)
            adv["bft_risk"]         = True
            adv["bft_at_risk"]      = at_risk
            adv["reasons"]          = list(adv.get("reasons", [])) + [
                f"MAVİ KUVVET RİSKİ: angajman koridorunda {asset_names}"
            ]
            bft_warnings.append({
                "track_id":   tid,
                "at_risk":    at_risk,
                "message":    f"[BFT] {tid}: {asset_names} angajman koridorunda — "
                              f"WEAPONS_FREE → WEAPONS_HOLD",
                "severity":   "CRITICAL",
            })

        safe_advisories.append(adv)

    return safe_advisories, bft_warnings
