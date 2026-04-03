"""
ai/zone_breach.py  —  Predictive zone breach detection

Checks Kalman-predicted trajectories against zone polygons.
If a predicted point (or its uncertainty ellipse) enters a zone,
emits a PREDICTIVE_BREACH warning with estimated time-to-breach.

Also exports helpers for uncertainty-cone polygon generation
(used by frontend for visualisation).
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Set, Tuple

# ── Constants ───────────────────────────────────────────────────────────────

DEG_TO_M = 111_320.0
SIGMA_SCALE = 2.0  # 2-sigma (~95% confidence) for breach check
COOLDOWN_S = 15.0  # don't repeat same breach warning within N seconds

# ── Helpers ─────────────────────────────────────────────────────────────────

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


def _nearest_polygon_dist_m(lat: float, lon: float, coords: List) -> float:
    """Approximate distance from point to nearest polygon vertex (meters)."""
    if _point_in_polygon(lat, lon, coords):
        return 0.0
    min_d = float("inf")
    cos_lat = math.cos(math.radians(lat))
    for c in coords:
        dlat = (c[0] - lat) * DEG_TO_M
        dlon = (c[1] - lon) * DEG_TO_M * cos_lat
        d = math.sqrt(dlat * dlat + dlon * dlon)
        if d < min_d:
            min_d = d
    return min_d


def _ellipse_intersects_polygon(
    lat: float, lon: float,
    sigma_lat: float, sigma_lon: float,
    coords: List,
    scale: float = SIGMA_SCALE,
) -> bool:
    """Check if the uncertainty ellipse (scaled) touches or enters the polygon.

    Fast approximation: check if center is inside, or if any polygon
    vertex is within the ellipse, or if any ellipse sample point is
    inside the polygon.
    """
    if _point_in_polygon(lat, lon, coords):
        return True

    # Check polygon vertices against ellipse
    slat = sigma_lat * scale
    slon = sigma_lon * scale
    if slat < 1e-12 and slon < 1e-12:
        return False

    for c in coords:
        dlat = (c[0] - lat) / slat if slat > 1e-12 else 1e12
        dlon = (c[1] - lon) / slon if slon > 1e-12 else 1e12
        if dlat * dlat + dlon * dlon <= 1.0:
            return True

    # Sample 8 points on the ellipse boundary and check against polygon
    for i in range(8):
        angle = i * math.pi / 4.0
        plat = lat + slat * math.cos(angle)
        plon = lon + slon * math.sin(angle)
        if _point_in_polygon(plat, plon, coords):
            return True

    return False


# ── Breach prediction engine ───────────────────────────────────────────────

# Cooldown registry: {key: last_emitted_time}
_cooldowns: Dict[str, float] = {}


def _should_emit(key: str) -> bool:
    last = _cooldowns.get(key, 0.0)
    now = time.time()
    if now - last < COOLDOWN_S:
        return False
    _cooldowns[key] = now
    return True


def check_predictive_breaches(
    predictions: Dict[str, List[Dict]],
    zones: Dict[str, Dict],
) -> List[Dict[str, Any]]:
    """
    Check all predicted trajectories against all zones.

    Args:
        predictions: {track_id: [{lat, lon, sigma_lat, sigma_lon, time_ahead_s}, ...]}
        zones: {zone_id: {id, name, type, coordinates: [[lat,lon], ...]}}

    Returns:
        List of breach warning dicts, sorted by urgency (earliest first).
    """
    if not predictions or not zones:
        return []

    warnings: List[Dict[str, Any]] = []
    now = time.time()

    for track_id, pred_points in predictions.items():
        if not pred_points:
            continue

        for zone_id, zone in zones.items():
            coords = zone.get("coordinates", [])
            if not coords or len(coords) < 3:
                continue

            # Check each predicted point
            for pt in pred_points:
                plat = pt.get("lat")
                plon = pt.get("lon")
                if plat is None or plon is None:
                    continue

                sigma_lat = pt.get("sigma_lat", 0.0)
                sigma_lon = pt.get("sigma_lon", 0.0)
                time_ahead = pt.get("time_ahead_s", 0)

                # Check center point first (deterministic breach)
                center_breach = _point_in_polygon(plat, plon, coords)

                # Then check uncertainty ellipse (probabilistic breach)
                ellipse_breach = False
                if not center_breach and sigma_lat > 0 and sigma_lon > 0:
                    ellipse_breach = _ellipse_intersects_polygon(
                        plat, plon, sigma_lat, sigma_lon, coords
                    )

                if center_breach or ellipse_breach:
                    key = f"PRED_BREACH:{track_id}:{zone_id}"
                    if not _should_emit(key):
                        break  # skip this track-zone pair entirely

                    confidence = "HIGH" if center_breach else "MEDIUM"
                    dist_m = _nearest_polygon_dist_m(
                        pred_points[0]["lat"], pred_points[0]["lon"], coords
                    )

                    warnings.append({
                        "type": "PREDICTIVE_BREACH",
                        "severity": "CRITICAL" if zone.get("type") == "kill" else "HIGH",
                        "confidence": confidence,
                        "track_id": track_id,
                        "zone_id": zone_id,
                        "zone_name": zone.get("name", zone_id),
                        "zone_type": zone.get("type", "restricted"),
                        "time_to_breach_s": time_ahead,
                        "predicted_lat": round(plat, 7),
                        "predicted_lon": round(plon, 7),
                        "sigma_lat": round(sigma_lat, 9),
                        "sigma_lon": round(sigma_lon, 9),
                        "current_distance_m": round(dist_m),
                        "message": (
                            f"{track_id} → {zone.get('name', zone_id)} "
                            f"({zone.get('type', 'restricted')}) "
                            f"tahmini ihlal: {time_ahead}s icinde "
                            f"[{confidence}] "
                            f"(mesafe: {round(dist_m)}m)"
                        ),
                        "time": now,
                    })
                    break  # first breach point is enough per track-zone pair

    # Sort by urgency: earliest breach first
    warnings.sort(key=lambda w: w["time_to_breach_s"])
    return warnings


# ── Uncertainty cone builder (for frontend) ─────────────────────────────────

def build_uncertainty_cones(
    predictions: Dict[str, List[Dict]],
) -> Dict[str, List[Dict]]:
    """
    Build uncertainty cone polygons for each track's predicted trajectory.

    Returns {track_id: [{lat, lon, sigma_lat_m, sigma_lon_m, time_ahead_s}, ...]}
    where sigma values are in meters for easier frontend rendering.
    """
    cones: Dict[str, List[Dict]] = {}
    for track_id, pts in predictions.items():
        if not pts:
            continue
        cone_pts = []
        for pt in pts:
            sigma_lat = pt.get("sigma_lat", 0.0)
            sigma_lon = pt.get("sigma_lon", 0.0)
            cos_lat = math.cos(math.radians(pt["lat"]))
            cone_pts.append({
                "lat": pt["lat"],
                "lon": pt["lon"],
                "sigma_lat_m": round(sigma_lat * DEG_TO_M, 1),
                "sigma_lon_m": round(sigma_lon * DEG_TO_M * cos_lat, 1),
                "time_ahead_s": pt["time_ahead_s"],
            })
        cones[track_id] = cone_pts
    return cones


# ── Lifecycle ───────────────────────────────────────────────────────────────

def reset() -> None:
    _cooldowns.clear()
