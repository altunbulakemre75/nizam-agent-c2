"""
ai/tactical.py  —  Tactical Recommendation Engine

Analyzes the full COP state (tracks, threats, assets, zones, anomalies,
predictions) and generates prioritized tactical recommendations for the
operator.

Recommendation types:
  - INTERCEPT    : assign friendly asset to engage a high-threat track
  - REPOSITION   : move friendly asset to cover a gap or threat axis
  - ESCALATE     : elevate alert level based on anomaly cluster
  - WITHDRAW     : pull back asset from kill zone or overwhelming swarm
  - MONITOR      : increase observation on suspicious track
  - ZONE_WARNING : threat approaching restricted/kill zone
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple

# ── Constants ───────────────────────────────────────────────────────────────

DEG_TO_M = 111_320.0

# Distances (meters)
INTERCEPT_RANGE_M    = 2000.0   # max distance for intercept assignment
ZONE_WARNING_DIST_M  = 500.0    # warn when threat is this close to a zone
REPOSITION_GAP_M     = 3000.0   # gap in coverage worth filling

# Cooldown: don't repeat same recommendation within N seconds
COOLDOWN_S = 30.0


# ── Helpers ─────────────────────────────────────────────────────────────────

def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = (lat2 - lat1) * DEG_TO_M
    dlon = (lon2 - lon1) * DEG_TO_M * math.cos(math.radians((lat1 + lat2) / 2))
    return math.sqrt(dlat * dlat + dlon * dlon)


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    """Human-readable bearing label (N/NE/E/...)."""
    dlat = lat2 - lat1
    dlon = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    deg = math.degrees(math.atan2(dlon, dlat)) % 360
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[int((deg + 22.5) / 45) % 8]


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


def _polygon_centroid(coords: List) -> Tuple[float, float]:
    if not coords:
        return (0.0, 0.0)
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    return (sum(lats) / len(lats), sum(lons) / len(lons))


def _nearest_polygon_dist(lat: float, lon: float, coords: List) -> float:
    """Approximate distance from point to polygon edge."""
    if _point_in_polygon(lat, lon, coords):
        return 0.0
    min_d = float("inf")
    for c in coords:
        d = _dist_m(lat, lon, c[0], c[1])
        if d < min_d:
            min_d = d
    return min_d


# ── Recommendation Engine ──────────────────────────────────────────────────

# Cooldown registry: {recommendation_key: last_emitted_time}
_cooldowns: Dict[str, float] = {}


def _should_emit(key: str) -> bool:
    last = _cooldowns.get(key, 0.0)
    if time.time() - last < COOLDOWN_S:
        return False
    _cooldowns[key] = time.time()
    return True


def generate_recommendations(
    tracks: Dict[str, Dict],
    threats: Dict[str, Dict],
    assets: Dict[str, Dict],
    zones: Dict[str, Dict],
    anomalies: List[Dict],
    predictions: Dict[str, List[Dict]],
) -> List[Dict[str, Any]]:
    """
    Analyze full COP state and produce tactical recommendations.
    Returns a prioritized list (highest priority first).
    """
    recs: List[Dict[str, Any]] = []
    now = time.time()

    # Classify assets
    friendlies = {k: v for k, v in assets.items()
                  if v.get("type") == "friendly" and v.get("status") == "active"}
    hostile_tracks = {k: v for k, v in tracks.items()
                      if threats.get(k, {}).get("threat_level") in ("HIGH", "MEDIUM")}

    # ── 1. INTERCEPT: assign nearest friendly to high threats ────────────
    for tid, track in hostile_tracks.items():
        threat = threats.get(tid, {})
        if threat.get("threat_level") != "HIGH":
            continue
        tlat, tlon = track.get("lat"), track.get("lon")
        if tlat is None or tlon is None:
            continue

        best_asset = None
        best_dist = float("inf")
        for aid, asset in friendlies.items():
            d = _dist_m(tlat, tlon, asset["lat"], asset["lon"])
            if d < INTERCEPT_RANGE_M and d < best_dist:
                best_dist = d
                best_asset = asset

        if best_asset:
            key = f"INTERCEPT:{tid}:{best_asset['id']}"
            if _should_emit(key):
                direction = _bearing(best_asset["lat"], best_asset["lon"], tlat, tlon)
                recs.append({
                    "type": "INTERCEPT",
                    "priority": 1,
                    "track_id": tid,
                    "asset_id": best_asset["id"],
                    "asset_name": best_asset.get("name", best_asset["id"]),
                    "distance_m": round(best_dist),
                    "direction": direction,
                    "message": f"{best_asset.get('name', best_asset['id'])} ile "
                               f"{tid} hedefini intercept et "
                               f"({round(best_dist)}m {direction})",
                    "time": now,
                })

    # ── 2. ZONE_WARNING: threat approaching protected zones ─────────────
    for tid, track in hostile_tracks.items():
        tlat, tlon = track.get("lat"), track.get("lon")
        if tlat is None or tlon is None:
            continue

        # Also check predicted future positions
        pred_points = [(tlat, tlon)]
        for p in predictions.get(tid, []):
            pred_points.append((p["lat"], p["lon"]))

        for zid, zone in zones.items():
            coords = zone.get("coordinates", [])
            if not coords:
                continue
            for plat, plon in pred_points:
                dist = _nearest_polygon_dist(plat, plon, coords)
                if dist <= ZONE_WARNING_DIST_M:
                    key = f"ZONE_WARNING:{tid}:{zid}"
                    if _should_emit(key):
                        inside = "ICINDE" if dist == 0 else f"{round(dist)}m uzakta"
                        recs.append({
                            "type": "ZONE_WARNING",
                            "priority": 2 if dist == 0 else 3,
                            "track_id": tid,
                            "zone_id": zid,
                            "zone_name": zone.get("name", zid),
                            "zone_type": zone.get("type", "restricted"),
                            "distance_m": round(dist),
                            "message": f"{tid} hedefi {zone.get('name', zid)} "
                                       f"({zone.get('type', 'restricted')}) bolgesi {inside}!",
                            "time": now,
                        })
                    break

    # ── 3. ESCALATE: anomaly-driven alert escalation ────────────────────
    critical_anomalies = [a for a in anomalies
                          if a.get("severity") in ("CRITICAL", "HIGH")]
    if critical_anomalies:
        swarm_anomalies = [a for a in critical_anomalies if a.get("type") == "SWARM_DETECTED"]
        for sa in swarm_anomalies:
            key = f"ESCALATE:SWARM:{','.join(sorted(sa.get('track_ids', [])))}"
            if _should_emit(key):
                recs.append({
                    "type": "ESCALATE",
                    "priority": 1,
                    "anomaly_type": "SWARM_DETECTED",
                    "track_ids": sa.get("track_ids", []),
                    "message": f"SWARM TESPIT EDILDI: {sa['count']} hedef koordineli hareket! "
                               f"Alarm seviyesini yukseltin.",
                    "lat": sa.get("lat"),
                    "lon": sa.get("lon"),
                    "time": now,
                })

        intent_shifts = [a for a in critical_anomalies if a.get("type") == "INTENT_SHIFT"]
        for ish in intent_shifts:
            key = f"ESCALATE:INTENT:{ish.get('track_id')}"
            if _should_emit(key):
                recs.append({
                    "type": "ESCALATE",
                    "priority": 2,
                    "anomaly_type": "INTENT_SHIFT",
                    "track_id": ish.get("track_id"),
                    "message": f"{ish['track_id']} hedefi saldiri moduna gecti! "
                               f"Dikkat: {ish.get('detail', '')}",
                    "time": now,
                })

    # ── 4. WITHDRAW: friendly inside kill zone or overwhelmed ───────────
    for aid, asset in friendlies.items():
        alat, alon = asset["lat"], asset["lon"]
        for zid, zone in zones.items():
            if zone.get("type") != "kill":
                continue
            coords = zone.get("coordinates", [])
            if _point_in_polygon(alat, alon, coords):
                key = f"WITHDRAW:{aid}:{zid}"
                if _should_emit(key):
                    recs.append({
                        "type": "WITHDRAW",
                        "priority": 2,
                        "asset_id": aid,
                        "asset_name": asset.get("name", aid),
                        "zone_id": zid,
                        "zone_name": zone.get("name", zid),
                        "message": f"{asset.get('name', aid)} kill zone "
                                   f"'{zone.get('name', zid)}' icinde! Geri cekilin.",
                        "time": now,
                    })

    # ── 5. MONITOR: medium threats without coverage ─────────────────────
    for tid, track in hostile_tracks.items():
        threat = threats.get(tid, {})
        if threat.get("threat_level") != "MEDIUM":
            continue
        tlat, tlon = track.get("lat"), track.get("lon")
        if tlat is None or tlon is None:
            continue

        # Check if any friendly is within monitoring range
        covered = False
        for aid, asset in friendlies.items():
            d = _dist_m(tlat, tlon, asset["lat"], asset["lon"])
            if d < INTERCEPT_RANGE_M * 1.5:
                covered = True
                break

        if not covered:
            key = f"MONITOR:{tid}"
            if _should_emit(key):
                recs.append({
                    "type": "MONITOR",
                    "priority": 4,
                    "track_id": tid,
                    "message": f"{tid} (MEDIUM tehdit) izlenmeden hareket ediyor. "
                               f"Gozetleme birimi atayin.",
                    "time": now,
                })

    # ── 6. REPOSITION: coverage gap detection ───────────────────────────
    if hostile_tracks and friendlies:
        # Find centroid of hostile tracks
        h_lats = [t["lat"] for t in hostile_tracks.values()
                  if t.get("lat") is not None]
        h_lons = [t["lon"] for t in hostile_tracks.values()
                  if t.get("lon") is not None]
        if h_lats:
            h_clat = sum(h_lats) / len(h_lats)
            h_clon = sum(h_lons) / len(h_lons)

            # Check if any friendly covers the centroid
            min_dist = float("inf")
            nearest_asset = None
            for aid, asset in friendlies.items():
                d = _dist_m(h_clat, h_clon, asset["lat"], asset["lon"])
                if d < min_dist:
                    min_dist = d
                    nearest_asset = asset

            if min_dist > REPOSITION_GAP_M and nearest_asset:
                key = "REPOSITION:centroid"
                if _should_emit(key):
                    direction = _bearing(nearest_asset["lat"], nearest_asset["lon"],
                                         h_clat, h_clon)
                    recs.append({
                        "type": "REPOSITION",
                        "priority": 3,
                        "asset_id": nearest_asset["id"],
                        "asset_name": nearest_asset.get("name", nearest_asset["id"]),
                        "target_lat": round(h_clat, 6),
                        "target_lon": round(h_clon, 6),
                        "distance_m": round(min_dist),
                        "direction": direction,
                        "message": f"{nearest_asset.get('name', nearest_asset['id'])} "
                                   f"birimini {direction} yonune {round(min_dist)}m "
                                   f"konuslandir — tehdit merkezini kapsama alin.",
                        "time": now,
                    })

    # Sort by priority (1 = highest)
    recs.sort(key=lambda r: r["priority"])
    return recs


# ── Lifecycle ───────────────────────────────────────────────────────────────

def reset() -> None:
    _cooldowns.clear()
