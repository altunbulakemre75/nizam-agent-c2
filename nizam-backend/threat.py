# threat.py
from __future__ import annotations

from typing import Any, Dict, List, Tuple, Optional
import math


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _in_circle_zone(track: Dict[str, Any], zone: Dict[str, Any]) -> bool:
    """
    zone = {"lat": float, "lon": float, "r_m": float}
    """
    try:
        tlat = float(track.get("lat"))
        tlon = float(track.get("lon"))
        zlat = float(zone.get("lat"))
        zlon = float(zone.get("lon"))
        r_m = float(zone.get("r_m"))
    except Exception:
        return False

    d = _haversine_m(tlat, tlon, zlat, zlon)
    return d <= r_m


def compute_threat(track: Dict[str, Any], now_ts: float, cfg: Dict[str, Any]) -> Tuple[int, List[str]]:
    """
    Baseline rule-based threat scoring (0-100).
    Reasons are short string tags.
    """
    reasons: List[str] = []
    score = 0.0

    # 1) Freshness / staleness contribution
    ttl_stale_s = float(cfg.get("ttl_stale_s", 10.0))
    last_ts = track.get("last_ts")
    if last_ts is not None:
        age = max(0.0, now_ts - float(last_ts))
        # age 0 -> +10, age >= stale -> +0
        freshness = _clamp(1.0 - (age / max(0.001, ttl_stale_s)), 0.0, 1.0)
        score += 10.0 * freshness
    else:
        # unknown timing: small baseline
        score += 2.0

    # 2) Speed contribution (optional)
    speed = track.get("speed_mps")
    if speed is not None:
        try:
            sp = max(0.0, float(speed))
            speed_max = float(cfg.get("speed_max_mps", 25.0))
            s_norm = _clamp(sp / max(0.001, speed_max), 0.0, 1.0)
            # up to +20
            score += 20.0 * s_norm
            if sp >= 8.0:
                reasons.append("fast")
        except Exception:
            pass

    # 3) Zone violation (high weight)
    zone = cfg.get("zone_circle")
    if isinstance(zone, dict) and zone:
        if _in_circle_zone(track, zone):
            score += 60.0
            reasons.append("zone_violation")

    # 4) Status modifiers
    status = (track.get("status") or "").upper()
    if status == "STALE":
        score *= 0.6
        reasons.append("stale")
    elif status == "DEAD":
        score = 0.0
        reasons = ["dead"]

    score = _clamp(score, 0.0, 100.0)
    return int(round(score)), reasons
