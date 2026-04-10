"""
ai/coordinated_attack.py  —  Coordinated Attack Detection

Detects multi-track convergence patterns that indicate planned attacks:

  CONVERGENCE       : 2+ tracks predicted to meet at the same point
  ZONE_PINCER       : 2+ tracks approaching the same zone from different angles
  ASSET_TARGETED    : 2+ tracks converging on a friendly asset

Uses Kalman-predicted trajectories to look ahead and detect coordination
before the attack materialises.
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
from ai._fast_math import pairwise_distances as _np_pairwise

# ── Constants ───────────────────────────────────────────────────────────────

DEG_TO_M = 111_320.0

# Detection thresholds
CONVERGENCE_RADIUS_M  = 600.0   # predicted points within this = converging
MIN_TRACKS_CONVERGE   = 2       # minimum tracks to declare coordinated attack
ANGLE_SPREAD_DEG      = 60.0    # min angular spread for pincer classification
ASSET_THREAT_RADIUS_M = 1500.0  # tracks predicted within this of a friendly asset

COOLDOWN_S = 20.0               # don't repeat same alert within N seconds

# ── Helpers ─────────────────────────────────────────────────────────────────

def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = (lat2 - lat1) * DEG_TO_M
    dlon = (lon2 - lon1) * DEG_TO_M * math.cos(math.radians((lat1 + lat2) / 2))
    return math.sqrt(dlat * dlat + dlon * dlon)


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Bearing from point 1 to point 2 in degrees [0, 360)."""
    dlat = lat2 - lat1
    dlon = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    return math.degrees(math.atan2(dlon, dlat)) % 360


def _angular_spread(bearings: List[float]) -> float:
    """Max angular separation among a set of bearings (degrees)."""
    if len(bearings) < 2:
        return 0.0
    best = 0.0
    for i in range(len(bearings)):
        for j in range(i + 1, len(bearings)):
            d = abs(bearings[i] - bearings[j]) % 360
            if d > 180:
                d = 360 - d
            if d > best:
                best = d
    return best


def _centroid(points: List[Tuple[float, float]]) -> Tuple[float, float]:
    if not points:
        return (0.0, 0.0)
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    return (sum(lats) / len(lats), sum(lons) / len(lons))


def _nearest_polygon_dist(lat: float, lon: float, coords: List) -> float:
    """Approximate distance from point to nearest polygon vertex."""
    min_d = float("inf")
    for c in coords:
        d = _dist_m(lat, lon, c[0], c[1])
        if d < min_d:
            min_d = d
    return min_d


# ── Cooldown ────────────────────────────────────────────────────────────────

_cooldowns: Dict[str, float] = {}


def _should_emit(key: str) -> bool:
    last = _cooldowns.get(key, 0.0)
    now = time.time()
    if now - last < COOLDOWN_S:
        return False
    _cooldowns[key] = now
    return True


# ── Core detection ──────────────────────────────────────────────────────────

def _find_trajectory_convergences(
    predictions: Dict[str, List[Dict]],
    tracks: Dict[str, Dict],
) -> List[Dict[str, Any]]:
    """
    Find groups of tracks whose predicted trajectories converge.

    For each future time step, cluster predicted positions. If 2+ tracks
    are within CONVERGENCE_RADIUS_M at the same future time, that's convergence.
    """
    if len(predictions) < MIN_TRACKS_CONVERGE:
        return []

    # Collect all track IDs that have predictions
    track_ids = [tid for tid, pts in predictions.items() if len(pts) >= 2]
    if len(track_ids) < MIN_TRACKS_CONVERGE:
        return []

    # For each time step, check all pairs
    # Predictions have time_ahead_s at 5, 10, 15, ... 60
    max_steps = min(len(predictions[track_ids[0]]), 12)
    convergences: List[Dict[str, Any]] = []
    found_groups: Set[str] = set()  # avoid duplicate groups

    for step in range(max_steps):
        # Gather predicted positions at this time step
        step_positions: List[Tuple[str, float, float, int]] = []
        for tid in track_ids:
            pts = predictions[tid]
            if step < len(pts):
                pt = pts[step]
                step_positions.append((tid, pt["lat"], pt["lon"], pt["time_ahead_s"]))

        if len(step_positions) < MIN_TRACKS_CONVERGE:
            continue

        # Numpy-accelerated clustering within CONVERGENCE_RADIUS_M
        n = len(step_positions)
        _sp_lats = np.array([sp[1] for sp in step_positions], dtype=np.float64)
        _sp_lons = np.array([sp[2] for sp in step_positions], dtype=np.float64)
        _dm = _np_pairwise(_sp_lats, _sp_lons)
        _adj = _dm <= CONVERGENCE_RADIUS_M
        np.fill_diagonal(_adj, False)

        visited: Set[int] = set()

        for i in range(n):
            if i in visited:
                continue
            group = [i]
            visited.add(i)
            queue = [i]
            while queue:
                cur = queue.pop(0)
                for j in np.where(_adj[cur])[0].tolist():
                    if j not in visited:
                        visited.add(j)
                        group.append(j)
                        queue.append(j)

            if len(group) >= MIN_TRACKS_CONVERGE:
                group_ids = sorted([step_positions[idx][0] for idx in group])
                group_key = "|".join(group_ids)
                if group_key in found_groups:
                    continue
                found_groups.add(group_key)

                # Compute convergence point and details
                conv_points = [(step_positions[idx][1], step_positions[idx][2])
                               for idx in group]
                conv_lat, conv_lon = _centroid(conv_points)
                time_ahead = step_positions[group[0]][3]

                # Compute approach bearings (from current position to convergence)
                bearings = []
                for tid in group_ids:
                    tr = tracks.get(tid)
                    if tr and tr.get("lat") is not None:
                        b = _bearing_deg(tr["lat"], tr["lon"], conv_lat, conv_lon)
                        bearings.append(b)

                spread = _angular_spread(bearings)
                is_pincer = spread >= ANGLE_SPREAD_DEG

                # Current distances from tracks to convergence point
                distances = {}
                for tid in group_ids:
                    tr = tracks.get(tid)
                    if tr and tr.get("lat") is not None:
                        distances[tid] = round(_dist_m(tr["lat"], tr["lon"],
                                                       conv_lat, conv_lon))

                convergences.append({
                    "track_ids": group_ids,
                    "convergence_lat": round(conv_lat, 7),
                    "convergence_lon": round(conv_lon, 7),
                    "time_to_convergence_s": time_ahead,
                    "angular_spread_deg": round(spread, 1),
                    "is_pincer": is_pincer,
                    "track_distances_m": distances,
                })

    return convergences


def detect_coordinated_attacks(
    tracks: Dict[str, Dict],
    predictions: Dict[str, List[Dict]],
    zones: Dict[str, Dict],
    assets: Dict[str, Dict],
) -> List[Dict[str, Any]]:
    """
    Detect coordinated attack patterns from predicted trajectories.

    Returns list of coordinated attack warning dicts, sorted by urgency.
    """
    if not predictions or len(predictions) < MIN_TRACKS_CONVERGE:
        return []

    warnings: List[Dict[str, Any]] = []
    now = time.time()

    # ── 1. Trajectory convergence detection ────────────────────────────
    convergences = _find_trajectory_convergences(predictions, tracks)

    for conv in convergences:
        tids = conv["track_ids"]
        key = f"CONVERGE:{','.join(tids)}"
        if not _should_emit(key):
            continue

        attack_type = "PINCER" if conv["is_pincer"] else "CONVERGENCE"
        severity = "CRITICAL" if conv["is_pincer"] or len(tids) >= 3 else "HIGH"

        warnings.append({
            "type": "COORDINATED_ATTACK",
            "subtype": attack_type,
            "severity": severity,
            "track_ids": tids,
            "count": len(tids),
            "convergence_lat": conv["convergence_lat"],
            "convergence_lon": conv["convergence_lon"],
            "time_to_convergence_s": conv["time_to_convergence_s"],
            "angular_spread_deg": conv["angular_spread_deg"],
            "track_distances_m": conv["track_distances_m"],
            "message": (
                f"KOORDINELI SALDIRI ({attack_type}): "
                f"{len(tids)} hedef {conv['time_to_convergence_s']}s icinde "
                f"yakinsamaya geciyor "
                f"(aci yayilimi: {conv['angular_spread_deg']}°)"
            ),
            "time": now,
        })

    # ── 2. Zone-targeted convergence ───────────────────────────────────
    for zid, zone in zones.items():
        coords = zone.get("coordinates", [])
        if not coords or len(coords) < 3:
            continue

        # Find tracks predicted to approach this zone
        approaching: List[Tuple[str, float, int]] = []  # (tid, min_dist, time_ahead)
        for tid, pts in predictions.items():
            for pt in pts:
                dist = _nearest_polygon_dist(pt["lat"], pt["lon"], coords)
                if dist <= CONVERGENCE_RADIUS_M:
                    approaching.append((tid, dist, pt["time_ahead_s"]))
                    break  # first predicted breach point is enough

        if len(approaching) >= MIN_TRACKS_CONVERGE:
            tids = sorted([a[0] for a in approaching])
            key = f"ZONE_PINCER:{zid}:{','.join(tids)}"
            if not _should_emit(key):
                continue

            # Compute approach bearings to zone centroid
            z_clat = sum(c[0] for c in coords) / len(coords)
            z_clon = sum(c[1] for c in coords) / len(coords)
            bearings = []
            for tid in tids:
                tr = tracks.get(tid)
                if tr and tr.get("lat") is not None:
                    bearings.append(_bearing_deg(tr["lat"], tr["lon"], z_clat, z_clon))
            spread = _angular_spread(bearings)
            is_pincer = spread >= ANGLE_SPREAD_DEG

            earliest = min(a[2] for a in approaching)
            subtype = "ZONE_PINCER" if is_pincer else "ZONE_CONVERGE"

            warnings.append({
                "type": "COORDINATED_ATTACK",
                "subtype": subtype,
                "severity": "CRITICAL",
                "track_ids": tids,
                "count": len(tids),
                "target_type": "zone",
                "target_id": zid,
                "target_name": zone.get("name", zid),
                "zone_type": zone.get("type", "restricted"),
                "convergence_lat": round(z_clat, 7),
                "convergence_lon": round(z_clon, 7),
                "time_to_convergence_s": earliest,
                "angular_spread_deg": round(spread, 1),
                "message": (
                    f"BOLGE HEDEFLI SALDIRI ({subtype}): "
                    f"{len(tids)} hedef '{zone.get('name', zid)}' "
                    f"({zone.get('type', 'restricted')}) bolgesine yaklasiyor "
                    f"— {earliest}s icinde "
                    f"(aci yayilimi: {round(spread, 1)}°)"
                ),
                "time": now,
            })

    # ── 3. Asset-targeted convergence ──────────────────────────────────
    friendlies = {k: v for k, v in assets.items()
                  if v.get("type") == "friendly" and v.get("status") == "active"}

    for aid, asset in friendlies.items():
        alat, alon = asset.get("lat"), asset.get("lon")
        if alat is None or alon is None:
            continue

        # Find tracks predicted to approach this asset
        approaching: List[Tuple[str, float, int]] = []
        for tid, pts in predictions.items():
            for pt in pts:
                dist = _dist_m(pt["lat"], pt["lon"], alat, alon)
                if dist <= ASSET_THREAT_RADIUS_M:
                    approaching.append((tid, dist, pt["time_ahead_s"]))
                    break

        if len(approaching) >= MIN_TRACKS_CONVERGE:
            tids = sorted([a[0] for a in approaching])
            key = f"ASSET_TARGET:{aid}:{','.join(tids)}"
            if not _should_emit(key):
                continue

            bearings = []
            for tid in tids:
                tr = tracks.get(tid)
                if tr and tr.get("lat") is not None:
                    bearings.append(_bearing_deg(tr["lat"], tr["lon"], alat, alon))
            spread = _angular_spread(bearings)
            is_pincer = spread >= ANGLE_SPREAD_DEG

            earliest = min(a[2] for a in approaching)
            subtype = "ASSET_PINCER" if is_pincer else "ASSET_CONVERGE"

            warnings.append({
                "type": "COORDINATED_ATTACK",
                "subtype": subtype,
                "severity": "CRITICAL",
                "track_ids": tids,
                "count": len(tids),
                "target_type": "asset",
                "target_id": aid,
                "target_name": asset.get("name", aid),
                "convergence_lat": round(alat, 7),
                "convergence_lon": round(alon, 7),
                "time_to_convergence_s": earliest,
                "angular_spread_deg": round(spread, 1),
                "message": (
                    f"ASSET HEDEFLI SALDIRI ({subtype}): "
                    f"{len(tids)} hedef '{asset.get('name', aid)}' "
                    f"birimini hedefliyor — {earliest}s icinde "
                    f"(aci yayilimi: {round(spread, 1)}°)"
                ),
                "time": now,
            })

    # Sort by urgency: earliest convergence first, then severity
    sev_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}
    warnings.sort(key=lambda w: (w["time_to_convergence_s"],
                                  sev_order.get(w["severity"], 9)))

    # Decision lineage: every participating track gets a record pointing at
    # the coordinated-attack pattern so threat provenance can be reconstructed.
    if warnings:
        try:
            from ai import lineage
            for w in warnings:
                for tid in w.get("track_ids", []):
                    lineage.record(
                        track_id=tid,
                        stage="coord_attack",
                        summary=(
                            f"{w['subtype']} ({w['severity']}) — "
                            f"{w['count']} tracks, {w['time_to_convergence_s']}s to convergence"
                        ),
                        inputs={
                            "participants": w["track_ids"],
                            "angular_spread_deg": w.get("angular_spread_deg"),
                            "target_type": w.get("target_type"),
                            "target_id": w.get("target_id"),
                        },
                        outputs={
                            "subtype": w["subtype"],
                            "severity": w["severity"],
                            "convergence_lat": w.get("convergence_lat"),
                            "convergence_lon": w.get("convergence_lon"),
                            "time_to_convergence_s": w.get("time_to_convergence_s"),
                        },
                        rule=f"coord_attack.{w['subtype'].lower()}",
                    )
        except Exception:
            pass

    return warnings


# ── Lifecycle ───────────────────────────────────────────────────────────────

def reset() -> None:
    _cooldowns.clear()
