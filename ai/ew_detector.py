"""
ai/ew_detector.py — Electronic Warfare (EW) threat detection

Detects three classes of EW attacks targeting the sensor network:

1. GPS_SPOOFING
   A track's reported position jumps an impossible distance in one update
   (faster than any known airborne platform). Indicates GPS signal spoofing
   or coordinate injection into the ingest stream.

   Threshold: > GPS_SPOOF_MAX_SPEED_MPS (500 m/s) between two consecutive
   updates. Commercial drones top out around 80 m/s; fighter jets ~600 m/s.
   We use 500 m/s as the "impossible for a drone" gate.

2. RADAR_JAMMING
   A previously active track stops sending updates for JAMMING_STALE_S
   seconds while an anomalous SNR drop is reported, or many tracks go
   stale simultaneously (mass jamming signature).

   Heuristic: if ≥ JAMMING_TRACK_COUNT tracks last seen > JAMMING_STALE_S
   ago go stale at the same time (within JAMMING_WINDOW_S), it is likely
   jamming rather than individual track drops.

3. FALSE_INJECTION
   An unusually high number of new tracks appear from a single sensor
   within a short window — characteristic of a replay/injection attack
   that floods the track table.

   Threshold: > INJECTION_RATE_PER_S new tracks attributed to a single
   sensor in INJECTION_WINDOW_S seconds.

Thread-safe (all shared state under _lock).

Usage:
    from ai import ew_detector

    # On every cop.track ingest:
    alerts = ew_detector.on_track_update(track_id, lat, lon, ts, sensors)
    if alerts:
        # broadcast cop.ew_alert for each

    # Periodically (e.g. from tactical background task):
    jamming = ew_detector.check_mass_jamming(all_tracks)
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

# ── Constants ────────────────────────────────────────────────────────────────

DEG_TO_M = 111_320.0

# GPS spoofing
GPS_SPOOF_MAX_SPEED_MPS = 500.0   # impossible speed gate (m/s)

# Radar jamming (mass stale detection)
JAMMING_STALE_S       = 8.0    # seconds without update to be "stale"
JAMMING_TRACK_COUNT   = 4      # min tracks stale simultaneously
JAMMING_WINDOW_S      = 5.0    # tracks must all go stale within this window

# False injection
INJECTION_WINDOW_S    = 10.0   # rolling window for new-track counting
INJECTION_RATE_THRESH = 8      # new tracks from one sensor in the window


# ── State ────────────────────────────────────────────────────────────────────

@dataclass
class _TrackRecord:
    lat:      float
    lon:      float
    ts:       float
    sensors:  List[str] = field(default_factory=list)


_lock = threading.Lock()
_tracks:  Dict[str, _TrackRecord] = {}         # track_id → last known state
_jamming_last_alert_ts: float = 0.0            # debounce mass-jamming alerts
_injection_events: Dict[str, Deque[float]] = {}  # sensor_id → deque of new-track timestamps


# ── Helpers ──────────────────────────────────────────────────────────────────

def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = (lat2 - lat1) * DEG_TO_M
    dlon = (lon2 - lon1) * DEG_TO_M * math.cos(math.radians((lat1 + lat2) / 2))
    return math.sqrt(dlat * dlat + dlon * dlon)


def _alert(alert_type: str, severity: str, track_id: Optional[str],
           detail: str, lat: Optional[float] = None,
           lon: Optional[float] = None, ts: Optional[float] = None) -> Dict[str, Any]:
    return {
        "type":     alert_type,
        "severity": severity,
        "track_id": track_id,
        "detail":   detail,
        "lat":      lat,
        "lon":      lon,
        "time":     ts or time.time(),
    }


# ── Public API ────────────────────────────────────────────────────────────────

def on_track_update(
    track_id: str,
    lat: float,
    lon: float,
    sensors: Optional[List[str]] = None,
    ts: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    Called on every cop.track ingest.
    Returns list of EW alert dicts (usually empty).
    """
    now = ts or time.time()
    alerts: List[Dict[str, Any]] = []
    sensors = sensors or []

    with _lock:
        prev = _tracks.get(track_id)

        if prev is not None:
            # ── 1) GPS Spoofing check ────────────────────────────────────────
            dt = now - prev.ts
            if dt > 0.01:
                dist = _dist_m(prev.lat, prev.lon, lat, lon)
                implied_speed = dist / dt
                if implied_speed > GPS_SPOOF_MAX_SPEED_MPS:
                    alerts.append(_alert(
                        "GPS_SPOOFING", "CRITICAL", track_id,
                        f"Position jump {dist:.0f} m in {dt:.2f}s "
                        f"(implied speed {implied_speed:.0f} m/s > "
                        f"{GPS_SPOOF_MAX_SPEED_MPS:.0f} m/s gate)",
                        lat, lon, now,
                    ))

        # ── 2) False injection check ─────────────────────────────────────────
        is_new_track = prev is None
        if is_new_track:
            for sensor in sensors:
                dq = _injection_events.setdefault(sensor, deque())
                # Evict old events outside window
                while dq and now - dq[0] > INJECTION_WINDOW_S:
                    dq.popleft()
                dq.append(now)
                if len(dq) > INJECTION_RATE_THRESH:
                    alerts.append(_alert(
                        "FALSE_INJECTION", "HIGH", None,
                        f"Sensor '{sensor}' created {len(dq)} new tracks "
                        f"in {INJECTION_WINDOW_S}s "
                        f"(threshold {INJECTION_RATE_THRESH})",
                        lat, lon, now,
                    ))

        # Update state
        _tracks[track_id] = _TrackRecord(lat=lat, lon=lon, ts=now, sensors=list(sensors))

    return alerts


def check_mass_jamming(all_tracks: Dict[str, Dict]) -> List[Dict[str, Any]]:
    """
    Inspect all COP tracks for simultaneous stale pattern.
    Call periodically (e.g. from tactical background task).
    Returns list of RADAR_JAMMING alert dicts (0 or 1 per call).
    """
    global _jamming_last_alert_ts

    now = time.time()
    alerts: List[Dict[str, Any]] = []

    with _lock:
        # Collect stale tracks: in our state but not updated in JAMMING_STALE_S
        stale: List[str] = []
        stale_ts_list: List[float] = []
        for tid, rec in _tracks.items():
            age = now - rec.ts
            if age >= JAMMING_STALE_S:
                stale.append(tid)
                stale_ts_list.append(rec.ts)

        if len(stale) < JAMMING_TRACK_COUNT:
            return []

        # Check they all went stale within JAMMING_WINDOW_S
        if stale_ts_list:
            earliest = min(stale_ts_list)
            latest   = max(stale_ts_list)
            if latest - earliest <= JAMMING_WINDOW_S:
                # Debounce: don't re-fire within 30s
                if now - _jamming_last_alert_ts > 30.0:
                    _jamming_last_alert_ts = now
                    alerts.append(_alert(
                        "RADAR_JAMMING", "CRITICAL", None,
                        f"{len(stale)} tracks went stale simultaneously "
                        f"(within {latest - earliest:.1f}s window). "
                        f"Possible radar jamming. Affected: {', '.join(stale[:6])}",
                        ts=now,
                    ))

    return alerts


def remove_track(track_id: str) -> None:
    with _lock:
        _tracks.pop(track_id, None)


def reset() -> None:
    global _jamming_last_alert_ts
    with _lock:
        _tracks.clear()
        _injection_events.clear()
        _jamming_last_alert_ts = 0.0


def stats() -> Dict[str, Any]:
    with _lock:
        return {
            "tracked_count":   len(_tracks),
            "sensor_windows":  {k: len(v) for k, v in _injection_events.items()},
        }
