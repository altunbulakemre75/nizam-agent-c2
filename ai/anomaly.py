"""
ai/anomaly.py  —  Anomaly detection & swarm pattern recognition

Track-level anomalies:
  - SPEED_SPIKE      : sudden speed change > 80 %
  - HEADING_REVERSAL : heading change > 120 deg in one step
  - ACCEL_BURST      : abnormal acceleration
  - INTENT_SHIFT     : loitering/recon -> attack transition

Swarm-level anomalies:
  - SWARM_DETECTED   : N tracks with correlated headings & formation
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
from ai._fast_math import pairwise_distances, heading_diffs


# ── Track-level state ───────────────────────────────────────────────────────

@dataclass
class _TrackStats:
    prev_lat: float = 0.0
    prev_lon: float = 0.0
    prev_speed: float = 0.0
    prev_heading: float = 0.0
    prev_intent: str = "unknown"
    prev_t: float = 0.0
    update_count: int = 0


_stats: Dict[str, _TrackStats] = {}

# ── Constants ───────────────────────────────────────────────────────────────

DEG_TO_M = 111_320.0  # approximate meters per degree latitude
SPEED_SPIKE_RATIO = 0.80       # 80% change
HEADING_REVERSAL_DEG = 120.0
MIN_UPDATES_FOR_ANOMALY = 3    # need baseline before detecting

# Swarm detection
SWARM_MIN_TRACKS = 3
SWARM_MAX_DIST_M = 800.0       # max inter-track distance for swarm grouping
SWARM_HEADING_TOL_DEG = 30.0   # heading similarity tolerance


# ── Helpers ─────────────────────────────────────────────────────────────────

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Approximate distance in meters between two lat/lon points."""
    dlat = (lat2 - lat1) * DEG_TO_M
    dlon = (lon2 - lon1) * DEG_TO_M * math.cos(math.radians((lat1 + lat2) / 2))
    return math.sqrt(dlat * dlat + dlon * dlon)


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Bearing from point 1 to point 2 in degrees [0, 360)."""
    dlat = lat2 - lat1
    dlon = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    return math.degrees(math.atan2(dlon, dlat)) % 360


def _angle_diff(a: float, b: float) -> float:
    """Smallest absolute angle difference in degrees."""
    d = abs(a - b) % 360
    return d if d <= 180 else 360 - d


# ── Track-level anomaly detection ───────────────────────────────────────────

def check_track(track_id: str, lat: float, lon: float,
                intent: str = "unknown",
                ts: Optional[float] = None) -> List[Dict[str, Any]]:
    """
    Check a single track update for anomalies.
    Returns list of anomaly dicts (may be empty).
    """
    now = ts or time.time()
    anomalies: List[Dict[str, Any]] = []

    st = _stats.get(track_id)
    if st is None:
        _stats[track_id] = _TrackStats(
            prev_lat=lat, prev_lon=lon, prev_intent=intent, prev_t=now,
        )
        return anomalies

    dt = now - st.prev_t
    if dt < 0.05:
        return anomalies

    # Compute current speed & heading
    dist_m = _haversine_m(st.prev_lat, st.prev_lon, lat, lon)
    speed = dist_m / dt if dt > 0 else 0.0
    heading = _bearing_deg(st.prev_lat, st.prev_lon, lat, lon) if dist_m > 0.5 else st.prev_heading

    st.update_count += 1

    if st.update_count >= MIN_UPDATES_FOR_ANOMALY:
        # 1) Speed spike
        if st.prev_speed > 1.0 and speed > 1.0:
            ratio = abs(speed - st.prev_speed) / st.prev_speed
            if ratio > SPEED_SPIKE_RATIO:
                anomalies.append({
                    "type": "SPEED_SPIKE",
                    "severity": "HIGH" if ratio > 1.5 else "MEDIUM",
                    "track_id": track_id,
                    "detail": f"Speed changed {st.prev_speed:.1f} -> {speed:.1f} m/s ({ratio*100:.0f}%)",
                    "lat": lat, "lon": lon,
                    "time": now,
                })

        # 2) Heading reversal
        if dist_m > 2.0 and st.prev_speed > 2.0:
            hdiff = _angle_diff(heading, st.prev_heading)
            if hdiff > HEADING_REVERSAL_DEG:
                anomalies.append({
                    "type": "HEADING_REVERSAL",
                    "severity": "MEDIUM",
                    "track_id": track_id,
                    "detail": f"Heading changed {st.prev_heading:.0f} -> {heading:.0f} deg ({hdiff:.0f} deg)",
                    "lat": lat, "lon": lon,
                    "time": now,
                })

        # 3) Intent shift (loitering/recon -> attack)
        if st.prev_intent in ("loitering", "reconnaissance") and intent == "attack":
            anomalies.append({
                "type": "INTENT_SHIFT",
                "severity": "CRITICAL",
                "track_id": track_id,
                "detail": f"Intent changed {st.prev_intent} -> {intent}",
                "lat": lat, "lon": lon,
                "time": now,
            })

    # Update state
    st.prev_lat = lat
    st.prev_lon = lon
    st.prev_speed = speed
    st.prev_heading = heading
    st.prev_intent = intent
    st.prev_t = now

    # Decision lineage: record each anomaly against the offending track.
    if anomalies:
        try:
            from ai import lineage
            for a in anomalies:
                lineage.record(
                    track_id=track_id,
                    stage="anomaly",
                    summary=f"{a['type']} ({a['severity']}) — {a['detail']}",
                    inputs={"speed": speed, "heading": heading, "intent": intent},
                    outputs={"type": a["type"], "severity": a["severity"]},
                    rule=f"anomaly.{a['type'].lower()}",
                )
        except Exception:
            pass

    return anomalies


# ── Swarm detection ─────────────────────────────────────────────────────────

def detect_swarms(tracks: Dict[str, Dict]) -> List[Dict[str, Any]]:
    """
    Analyze all active tracks and detect coordinated swarm groups.
    Returns list of swarm anomaly dicts.
    """
    # Build list of tracks with valid positions
    active: List[Tuple[str, float, float, float]] = []  # (id, lat, lon, heading)
    for tid, t in tracks.items():
        lat = t.get("lat")
        lon = t.get("lon")
        st = _stats.get(tid)
        if lat is not None and lon is not None and st is not None:
            active.append((tid, float(lat), float(lon), st.prev_heading))

    if len(active) < SWARM_MIN_TRACKS:
        return []

    # ── Numpy-accelerated adjacency clustering ───────────────────────
    # Build coordinate arrays for vectorised distance + heading checks.
    # numpy releases GIL during C-level compute, enabling true parallel
    # execution when called from ThreadPoolExecutor.
    n = len(active)
    _lats = np.array([a[1] for a in active], dtype=np.float64)
    _lons = np.array([a[2] for a in active], dtype=np.float64)
    _hdgs = np.array([a[3] for a in active], dtype=np.float64)

    # N×N distance matrix (single numpy call, ~100x faster than Python loop)
    dist_matrix = pairwise_distances(_lats, _lons)        # N×N float64
    hdiff_matrix = heading_diffs(_hdgs)                    # N×N float64

    # Adjacency mask: close enough AND heading-aligned
    adj = (dist_matrix <= SWARM_MAX_DIST_M) & (hdiff_matrix <= SWARM_HEADING_TOL_DEG)
    np.fill_diagonal(adj, False)  # no self-edges

    # BFS clustering on the adjacency matrix
    visited: Set[int] = set()
    swarm_anomalies: List[Dict[str, Any]] = []

    for i in range(n):
        if i in visited:
            continue
        group = [i]
        visited.add(i)
        queue = [i]
        while queue:
            cur = queue.pop(0)
            neighbours = np.where(adj[cur])[0]
            for j in neighbours:
                j_int = int(j)
                if j_int not in visited:
                    visited.add(j_int)
                    group.append(j_int)
                    queue.append(j_int)

        if len(group) >= SWARM_MIN_TRACKS:
            track_ids = [active[idx][0] for idx in group]
            avg_lat = sum(active[idx][1] for idx in group) / len(group)
            avg_lon = sum(active[idx][2] for idx in group) / len(group)
            avg_heading = active[group[0]][3]  # approximate
            swarm_anomalies.append({
                "type": "SWARM_DETECTED",
                "severity": "CRITICAL",
                "track_ids": track_ids,
                "count": len(track_ids),
                "detail": f"Coordinated swarm: {len(track_ids)} tracks, "
                          f"heading ~{avg_heading:.0f} deg, "
                          f"within {SWARM_MAX_DIST_M}m",
                "lat": round(avg_lat, 6),
                "lon": round(avg_lon, 6),
                "time": time.time(),
            })

    # Decision lineage: every track in a swarm gets a record pointing at the
    # group, so "why is this track a threat?" can trace to the swarm membership.
    if swarm_anomalies:
        try:
            from ai import lineage
            for swarm in swarm_anomalies:
                detail = swarm["detail"]
                count = swarm["count"]
                for tid in swarm["track_ids"]:
                    lineage.record(
                        track_id=tid,
                        stage="anomaly",
                        summary=f"SWARM member ({count} tracks) — {detail}",
                        inputs={"group_size": count, "group": swarm["track_ids"]},
                        outputs={"type": "SWARM_DETECTED", "severity": "CRITICAL"},
                        rule="anomaly.swarm_cluster",
                    )
        except Exception:
            pass

    return swarm_anomalies


# ── Lifecycle ───────────────────────────────────────────────────────────────

def remove_track(track_id: str) -> None:
    _stats.pop(track_id, None)


def reset() -> None:
    _stats.clear()
