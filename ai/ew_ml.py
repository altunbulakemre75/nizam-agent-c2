"""
ai/ew_ml.py — Statistical / ML-based EW threat classifiers

Augments the rule-based ai/ew_detector.py with four higher-fidelity detectors:

1. SpeedZScoreDetector
   Rolls a speed history per track (last SPEED_HISTORY_N observations).
   Flags when the current implied speed deviates > ZSCORE_THRESHOLD σ from
   the track's own mean, even when the hard 500 m/s gate is not crossed.
   Catches "meaconing" — spoofing that nudges positions gradually over time.

2. TrajectoryDeviationDetector
   Dead-reckoning: given last position + speed + heading, extrapolates the
   expected next position. Flags when actual position deviates >
   DEVIATION_THRESHOLD_M metres from prediction.
   Catches spoofing that respects speed limits but ignores heading continuity.

3. CoordinatedSpoofDetector
   Watches for multiple tracks with correlated position-jump vectors within
   CORR_WINDOW_S. If >= CORR_MIN_TRACKS tracks all jump in roughly the same
   direction (within CORR_HEADING_TOL_DEG) the same absolute distance (within
   CORR_DIST_TOL_M), it is likely a single attacker injecting a vector error
   into many tracks simultaneously.

4. JammingSweepDetector
   Determines whether recently-stalled tracks' last-known positions lie along
   a linear corridor — signature of a mobile jammer sweeping across an area.
   Uses principal-component analysis (via covariance eigen-decomposition) on
   the positions and flags if the explained variance ratio > SWEEP_VARIANCE_RATIO
   (most variance in one direction — a line rather than a cluster).

All detectors are thread-safe and can be reset independently.
Integration: call on_track_update() for every ingest; call check_patterns() from
the tactical background task alongside ew_detector.check_mass_jamming().
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np

# ── Constants ─────────────────────────────────────────────────────────────────

DEG_TO_M = 111_320.0

# SpeedZScoreDetector
SPEED_HISTORY_N    = 20       # rolling window length per track
ZSCORE_THRESHOLD   = 4.0      # sigma for anomaly flag
SPEED_MIN_SAMPLES  = 5        # need at least this many samples to compute Z

# TrajectoryDeviationDetector
DEVIATION_THRESHOLD_M  = 800.0   # metres deviation from dead-reckoning
DEVIATION_MAX_DT_S     = 30.0    # don't extrapolate beyond this interval

# CoordinatedSpoofDetector
CORR_WINDOW_S         = 3.0     # seconds: simultaneous jump window
CORR_MIN_TRACKS       = 3       # minimum concurrent jumps to flag
CORR_HEADING_TOL_DEG  = 25.0    # heading agreement tolerance
CORR_DIST_TOL_M       = 200.0   # distance agreement tolerance

# JammingSweepDetector
SWEEP_STALE_S          = 8.0    # track considered stale for sweep analysis
SWEEP_MIN_TRACKS       = 4      # minimum stale tracks for sweep test
SWEEP_VARIANCE_RATIO   = 0.85   # PCA: fraction of variance in first component
SWEEP_DEBOUNCE_S       = 30.0   # re-fire cooldown


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = (lat2 - lat1) * DEG_TO_M
    mid_lat = math.radians((lat1 + lat2) / 2)
    dlon = (lon2 - lon1) * DEG_TO_M * math.cos(mid_lat)
    return math.sqrt(dlat * dlat + dlon * dlon)


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Bearing in [0, 360) from point 1 to point 2."""
    dlat = (lat2 - lat1) * DEG_TO_M
    mid_lat = math.radians((lat1 + lat2) / 2)
    dlon = (lon2 - lon1) * DEG_TO_M * math.cos(mid_lat)
    deg = math.degrees(math.atan2(dlon, dlat)) % 360
    return deg


def _heading_diff(a: float, b: float) -> float:
    """Absolute angular difference, wrapping at 180°."""
    d = abs(a - b) % 360
    return d if d <= 180 else 360 - d


def _ew_alert(alert_type: str, severity: str, track_id: Optional[str],
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
        "source":   "ew_ml",
    }


# ── State records ──────────────────────────────────────────────────────────────

@dataclass
class _TrackState:
    lat:      float
    lon:      float
    ts:       float
    speed_ms: float         # speed reported or implied (m/s)
    heading:  float         # degrees, 0 = north
    # Rolling implied-speed history for Z-score
    speed_history: Deque[float] = field(default_factory=lambda: deque(maxlen=SPEED_HISTORY_N))


# ── Per-track state ────────────────────────────────────────────────────────────

_lock   = threading.Lock()
_tracks: Dict[str, _TrackState] = {}

# Coordinated spoof: recent jump events {track_id: (bearing, distance, ts)}
_jump_events: Dict[str, Tuple[float, float, float]] = {}

# Sweep debounce
_sweep_last_alert_ts: float = 0.0


# ── 1. SpeedZScoreDetector ────────────────────────────────────────────────────

def _speed_zscore_check(
    track_id: str,
    prev: _TrackState,
    lat: float,
    lon: float,
    ts: float,
    current_speed: float,
) -> Optional[Dict[str, Any]]:
    """
    Return alert if current_speed is a Z-score outlier vs the PRIOR history.
    current_speed must be passed in pre-computed and NOT yet appended to history,
    so the baseline statistics are uncontaminated by the anomalous point.
    """
    history = list(prev.speed_history)
    if len(history) < SPEED_MIN_SAMPLES:
        return None

    mean = sum(history) / len(history)
    var  = sum((x - mean) ** 2 for x in history) / len(history)
    std  = math.sqrt(var) if var > 0 else 0.0
    # Apply a minimum std floor of 5% of mean so the detector works even for
    # perfectly steady tracks (GPS jitter in production always adds variance)
    std  = max(std, mean * 0.05, 0.5)
    if mean < 0.5:
        return None   # stationary track — no meaningful speed history

    z = (current_speed - mean) / std
    if z > ZSCORE_THRESHOLD:
        return _ew_alert(
            "GPS_SPOOFING_GRADUAL", "HIGH", track_id,
            f"Speed Z-score {z:.1f}σ (speed={current_speed:.1f} m/s, "
            f"track mean={mean:.1f} m/s, std={std:.1f} m/s)",
            lat, lon, ts,
        )
    return None


# ── 2. TrajectoryDeviationDetector ───────────────────────────────────────────

def _trajectory_deviation_check(
    track_id: str,
    prev: _TrackState,
    lat: float,
    lon: float,
    ts: float,
) -> Optional[Dict[str, Any]]:
    """Return alert if actual position deviates too far from dead-reckoning."""
    dt = ts - prev.ts
    if dt <= 0 or dt > DEVIATION_MAX_DT_S:
        return None
    speed   = prev.speed_ms
    heading = prev.heading
    if speed < 1.0:
        return None   # stationary — dead reckoning not meaningful

    # Dead-reckoning: project in heading direction
    dx_m = speed * dt * math.sin(math.radians(heading))  # east component
    dy_m = speed * dt * math.cos(math.radians(heading))  # north component
    pred_lat = prev.lat + dy_m / DEG_TO_M
    pred_lon = prev.lon + dx_m / (DEG_TO_M * math.cos(math.radians(prev.lat)))

    deviation = _dist_m(pred_lat, pred_lon, lat, lon)
    if deviation > DEVIATION_THRESHOLD_M:
        return _ew_alert(
            "TRAJECTORY_DEVIATION", "HIGH", track_id,
            f"Position {deviation:.0f} m from dead-reckoning prediction "
            f"(speed={speed:.1f} m/s, heading={heading:.0f}°, dt={dt:.1f}s)",
            lat, lon, ts,
        )
    return None


# ── 3. CoordinatedSpoofDetector ───────────────────────────────────────────────

def _coordinated_spoof_check(
    track_id: str,
    prev: _TrackState,
    lat: float,
    lon: float,
    ts: float,
) -> Optional[Dict[str, Any]]:
    """
    Record a large position jump; check if it correlates with other recent jumps.
    Returns alert if N tracks jump together in the same direction.
    """
    dt = ts - prev.ts
    if dt <= 0:
        return None
    dist  = _dist_m(prev.lat, prev.lon, lat, lon)
    if dist < 100.0:   # small movements are noise, not jumps
        return None

    bearing = _bearing_deg(prev.lat, prev.lon, lat, lon)

    # Record this jump
    _jump_events[track_id] = (bearing, dist, ts)

    # Prune old events outside the correlation window
    cutoff = ts - CORR_WINDOW_S
    stale = [t for t, (_, _, jts) in _jump_events.items() if jts < cutoff]
    for t in stale:
        _jump_events.pop(t, None)

    if len(_jump_events) < CORR_MIN_TRACKS:
        return None

    # Check if current jump agrees with the majority
    matching = []
    for t, (b, d, _) in _jump_events.items():
        if (_heading_diff(bearing, b) <= CORR_HEADING_TOL_DEG and
                abs(dist - d) <= CORR_DIST_TOL_M):
            matching.append(t)

    if len(matching) >= CORR_MIN_TRACKS:
        return _ew_alert(
            "COORDINATED_SPOOF", "CRITICAL", None,
            f"{len(matching)} tracks jumped {dist:.0f} m in bearing "
            f"{bearing:.0f}° within {CORR_WINDOW_S}s window — "
            f"likely coordinated GPS vector injection. "
            f"Affected tracks: {', '.join(sorted(matching)[:6])}",
            lat, lon, ts,
        )
    return None


# ── 4. JammingSweepDetector ───────────────────────────────────────────────────

def _jamming_sweep_check(all_tracks: Dict[str, Dict]) -> Optional[Dict[str, Any]]:
    """
    PCA-based sweep corridor detection.
    Called from check_patterns() together with data from COP STATE.
    """
    global _sweep_last_alert_ts
    now = time.time()

    # Collect last-known positions of stale EW-tracked tracks
    with _lock:
        stale_positions: List[Tuple[float, float]] = []
        for tid, rec in _tracks.items():
            if now - rec.ts >= SWEEP_STALE_S:
                stale_positions.append((rec.lat, rec.lon))

    if len(stale_positions) < SWEEP_MIN_TRACKS:
        return None

    # Debounce
    if now - _sweep_last_alert_ts < SWEEP_DEBOUNCE_S:
        return None

    # Convert to metres (relative offsets) for PCA
    lats = np.array([p[0] for p in stale_positions])
    lons = np.array([p[1] for p in stale_positions])
    mid_lat = math.radians(float(lats.mean()))

    x = (lons - lons.mean()) * DEG_TO_M * math.cos(mid_lat)
    y = (lats - lats.mean()) * DEG_TO_M

    pts = np.column_stack([x, y])
    cov = np.cov(pts.T)
    if cov.shape != (2, 2):
        return None

    eigenvalues = np.linalg.eigvalsh(cov)
    if eigenvalues.sum() < 1e-6:
        return None   # all tracks at same point

    explained = float(eigenvalues.max() / eigenvalues.sum())
    if explained >= SWEEP_VARIANCE_RATIO:
        _sweep_last_alert_ts = now
        spread_m = float(np.sqrt(eigenvalues.max()))
        return _ew_alert(
            "JAMMING_SWEEP", "HIGH", None,
            f"{len(stale_positions)} stale tracks form a linear corridor "
            f"(PCA explained variance {explained:.0%}, corridor ~{spread_m:.0f} m). "
            f"Possible mobile jamming source.",
            ts=now,
        )
    return None


# ── Public API ────────────────────────────────────────────────────────────────

def on_track_update(
    track_id: str,
    lat: float,
    lon: float,
    speed_ms: float = 0.0,
    heading: float = 0.0,
    ts: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    Call on every cop.track ingest — augments ew_detector with ML checks.
    Returns list of alert dicts (may be empty).
    """
    now = ts or time.time()
    alerts: List[Dict[str, Any]] = []

    with _lock:
        prev = _tracks.get(track_id)

        if prev is not None:
            dt = now - prev.ts
            implied: Optional[float] = None
            if dt > 0.01:
                implied = _dist_m(prev.lat, prev.lon, lat, lon) / dt

            # Z-score check uses history WITHOUT the current point appended yet
            if implied is not None:
                a = _speed_zscore_check(track_id, prev, lat, lon, now, implied)
                if a:
                    alerts.append(a)
                # Append after the check so the baseline stays uncontaminated
                prev.speed_history.append(implied)

            a = _trajectory_deviation_check(track_id, prev, lat, lon, now)
            if a:
                alerts.append(a)

            a = _coordinated_spoof_check(track_id, prev, lat, lon, now)
            if a:
                alerts.append(a)

        # Update state
        state = _TrackState(
            lat=lat, lon=lon, ts=now,
            speed_ms=speed_ms, heading=heading,
        )
        if prev is not None:
            # Carry over speed history
            state.speed_history = prev.speed_history
        _tracks[track_id] = state

    return alerts


def check_patterns(_all_tracks: Dict[str, Dict]) -> List[Dict[str, Any]]:
    """
    Call from the tactical background task.
    Runs sweep detection and returns any new pattern alerts.
    """
    alerts: List[Dict[str, Any]] = []
    a = _jamming_sweep_check(_all_tracks)
    if a:
        alerts.append(a)
    return alerts


def remove_track(track_id: str) -> None:
    with _lock:
        _tracks.pop(track_id, None)
        _jump_events.pop(track_id, None)


def reset() -> None:
    global _sweep_last_alert_ts
    with _lock:
        _tracks.clear()
        _jump_events.clear()
    _sweep_last_alert_ts = 0.0


def stats() -> Dict[str, Any]:
    with _lock:
        return {
            "tracked_count":   len(_tracks),
            "jump_events":     len(_jump_events),
            "sweep_last_alert_ago_s": round(time.time() - _sweep_last_alert_ts, 1),
            "config": {
                "zscore_threshold":     ZSCORE_THRESHOLD,
                "deviation_threshold_m": DEVIATION_THRESHOLD_M,
                "corr_min_tracks":      CORR_MIN_TRACKS,
                "sweep_variance_ratio": SWEEP_VARIANCE_RATIO,
            },
        }
