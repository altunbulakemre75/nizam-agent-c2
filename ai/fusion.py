"""
ai/fusion.py  —  Multi-sensor Track Fusion Engine for NIZAM

Implements a sensor-agnostic track fusion pipeline:

  1. Sensor Registry
     Each sensor has a covariance profile (position accuracy, speed accuracy)
     that weights its contribution to the fused track state.

  2. Measurement Pre-processing
     Raw reports (lat/lon/alt/speed/heading) are normalised to a flat-earth
     Cartesian frame (East-North-Up, metres) around a reference point so that
     standard linear algebra applies.

  3. GNN Association (Global Nearest Neighbour)
     Incoming sensor report → find nearest existing fused track within a
     gate distance.  Multiple reports from different sensors to the same
     physical target are merged; a new report outside all gates spawns a
     fresh fused track.

  4. Covariance-Weighted State Fusion (Kalman Information Filter style)
     Each sensor contributes a measurement with its own measurement noise
     covariance R_i.  The fused state x̂ and its covariance P are updated via
     the information filter (inverse covariance weighting):

         P_fused⁻¹ = Σ  R_i⁻¹
         x̂_fused   = P_fused · Σ (R_i⁻¹ · z_i)

  5. Output
     Each FusedTrack carries:
       - fused lat/lon/alt/speed/heading (best estimate)
       - contributing_sensors  list of sensor IDs
       - covariance estimate   [pos_m, speed_mps]
       - last_update           UTC ISO timestamp
       - sensor_reports        raw per-sensor measurements (for audit)

Architecture note
-----------------
FusionEngine is intentionally stateless across restarts — it only holds tracks
in memory, matching the rest of NIZAM's in-memory-first design.  Persistence
can be layered via the DB if needed.

Usage (standalone or wired via server.py):

    from ai.fusion import FusionEngine, SensorProfile

    engine = FusionEngine()
    engine.register_sensor("radar-north", SensorProfile(pos_std_m=50,  speed_std_mps=3))
    engine.register_sensor("iff-1",       SensorProfile(pos_std_m=100, speed_std_mps=5))
    engine.register_sensor("cot-adapter", SensorProfile(pos_std_m=200, speed_std_mps=10))

    fused = engine.update(
        sensor_id="radar-north",
        track_id_hint="TRK-001",
        lat=41.015, lon=28.979, alt_m=3000,
        speed_mps=250, heading_deg=90,
    )
    print(fused.lat, fused.lon, fused.contributing_sensors)
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    from shared.utils import utc_now_iso
except ImportError:
    from datetime import datetime, timezone
    def utc_now_iso() -> str:  # type: ignore[misc]
        return datetime.now(timezone.utc).isoformat()


# ── Constants ─────────────────────────────────────────────────────────────────

# Default gate distance: reports within this radius are candidates for the
# same physical target.
DEFAULT_GATE_M: float = 2000.0   # 2 km

# Track staleness: fused tracks with no update in this many seconds are removed.
DEFAULT_STALE_S: float = 120.0

# Minimum sensor variance floor (avoids division by zero / overconfidence).
_MIN_VAR: float = 1e-6

# Earth radius (metres) for lat/lon ↔ metres conversion.
_R_EARTH_M: float = 6_371_000.0


# ── Sensor profile ────────────────────────────────────────────────────────────

@dataclass
class SensorProfile:
    """
    Measurement noise model for one sensor type.

    pos_std_m      1-sigma position error (metres)   — used as √R_pos
    speed_std_mps  1-sigma speed error (m/s)          — used as √R_speed
    heading_std_deg 1-sigma heading error (degrees)
    priority       tie-break weight when multiple sensors update simultaneously
    """
    pos_std_m:       float = 100.0
    speed_std_mps:   float = 5.0
    heading_std_deg: float = 5.0
    priority:        float = 1.0

    @property
    def pos_var(self) -> float:
        return max(self.pos_std_m ** 2, _MIN_VAR)

    @property
    def speed_var(self) -> float:
        return max(self.speed_std_mps ** 2, _MIN_VAR)

    @property
    def heading_var(self) -> float:
        return max(self.heading_std_deg ** 2, _MIN_VAR)


# Default profiles for common sensor types (can be overridden)
SENSOR_PROFILES: Dict[str, SensorProfile] = {
    "radar":        SensorProfile(pos_std_m=30,   speed_std_mps=1.5, heading_std_deg=1.0),
    "iff":          SensorProfile(pos_std_m=50,   speed_std_mps=3.0, heading_std_deg=2.0),
    "adsb":         SensorProfile(pos_std_m=15,   speed_std_mps=1.0, heading_std_deg=0.5, priority=1.2),
    "cot":          SensorProfile(pos_std_m=200,  speed_std_mps=10,  heading_std_deg=10),
    "mqtt":         SensorProfile(pos_std_m=100,  speed_std_mps=5.0, heading_std_deg=5.0),
    "ais":          SensorProfile(pos_std_m=20,   speed_std_mps=0.5, heading_std_deg=1.0),
    "rest":         SensorProfile(pos_std_m=500,  speed_std_mps=20,  heading_std_deg=20),
    "manual":       SensorProfile(pos_std_m=1000, speed_std_mps=50,  heading_std_deg=30, priority=0.5),
}


# ── Measurement ───────────────────────────────────────────────────────────────

@dataclass
class SensorMeasurement:
    """One position/kinematics report from a single sensor."""
    sensor_id:   str
    track_hint:  str        # caller-provided track ID (may differ from fused ID)
    lat:         float
    lon:         float
    alt_m:       float      = 0.0
    speed_mps:   float      = 0.0
    heading_deg: float      = 0.0
    timestamp:   str        = ""
    raw:         Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = utc_now_iso()


# ── Fused track ───────────────────────────────────────────────────────────────

@dataclass
class FusedTrack:
    """
    The fused best-estimate of a physical target's state, built from
    one or more sensor reports.
    """
    id:                   str
    lat:                  float
    lon:                  float
    alt_m:                float
    speed_mps:            float
    heading_deg:          float
    contributing_sensors: List[str]
    pos_std_m:            float      = 0.0
    speed_std_mps:        float      = 0.0
    last_update:          str        = ""
    sensor_reports:       List[Dict[str, Any]] = field(default_factory=list)

    # Internal: running information-filter accumulators
    # (info_pos, info_speed, info_heading) = Σ (z/R) sums
    # (inv_R_pos, inv_R_speed, inv_R_heading) = Σ (1/R) sums
    _info_x:      float = field(default=0.0, repr=False)   # east (m)
    _info_y:      float = field(default=0.0, repr=False)   # north (m)
    _info_z:      float = field(default=0.0, repr=False)   # alt (m)
    _info_sp:     float = field(default=0.0, repr=False)   # speed
    _info_hd:     float = field(default=0.0, repr=False)   # heading
    _inv_R_pos:   float = field(default=0.0, repr=False)
    _inv_R_speed: float = field(default=0.0, repr=False)
    _inv_R_hd:    float = field(default=0.0, repr=False)
    _ref_lat:     float = field(default=0.0, repr=False)
    _ref_lon:     float = field(default=0.0, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id":                   self.id,
            "lat":                  round(self.lat,  7),
            "lon":                  round(self.lon,  7),
            "alt_m":                round(self.alt_m, 1),
            "speed_mps":            round(self.speed_mps,    2),
            "heading_deg":          round(self.heading_deg,  1),
            "pos_std_m":            round(self.pos_std_m,    1),
            "speed_std_mps":        round(self.speed_std_mps, 2),
            "contributing_sensors": list(self.contributing_sensors),
            "sensor_count":         len(self.contributing_sensors),
            "last_update":          self.last_update,
            "sensor_reports":       self.sensor_reports[-10:],  # cap for API
        }


# ── Coordinate helpers ────────────────────────────────────────────────────────

def _to_enu(lat: float, lon: float, alt: float,
            ref_lat: float, ref_lon: float) -> Tuple[float, float, float]:
    """Convert (lat,lon,alt) to local ENU metres relative to reference point."""
    dlat = math.radians(lat - ref_lat)
    dlon = math.radians(lon - ref_lon)
    cos_ref = math.cos(math.radians(ref_lat))
    east  = _R_EARTH_M * dlon * cos_ref
    north = _R_EARTH_M * dlat
    return east, north, alt   # alt is already metres


def _from_enu(east: float, north: float, alt: float,
              ref_lat: float, ref_lon: float) -> Tuple[float, float, float]:
    """Convert ENU metres back to (lat, lon, alt)."""
    cos_ref = math.cos(math.radians(ref_lat))
    lat = ref_lat + math.degrees(north / _R_EARTH_M)
    lon = ref_lon + math.degrees(east  / (_R_EARTH_M * cos_ref))
    return lat, lon, alt


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Fast flat-earth Euclidean distance in metres."""
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    cos_lat = math.cos(math.radians((lat1 + lat2) / 2))
    dy = _R_EARTH_M * dlat
    dx = _R_EARTH_M * dlon * cos_lat
    return math.sqrt(dx * dx + dy * dy)


def _angle_diff(a: float, b: float) -> float:
    """Signed angular difference (degrees), [-180, 180]."""
    d = (a - b + 540) % 360 - 180
    return d


# ── Fusion engine ─────────────────────────────────────────────────────────────

class FusionEngine:
    """
    Stateful multi-sensor track fusion engine.

    Thread-safety: this class is not thread-safe.  In server.py it is
    called from inside the ingest endpoint which runs under STATE_LOCK,
    so no additional locking is needed.
    """

    def __init__(
        self,
        gate_m: float       = DEFAULT_GATE_M,
        stale_s: float      = DEFAULT_STALE_S,
    ) -> None:
        self.gate_m   = gate_m
        self.stale_s  = stale_s

        self._tracks: Dict[str, FusedTrack] = {}          # fused_id → FusedTrack
        self._hint_map: Dict[str, str]       = {}          # sensor_hint → fused_id
        self._sensor_profiles: Dict[str, SensorProfile] = dict(SENSOR_PROFILES)

    # ── Sensor registry ────────────────────────────────────────────────────

    def register_sensor(self, sensor_id: str, profile: SensorProfile) -> None:
        """Register or update a sensor profile."""
        self._sensor_profiles[sensor_id] = profile

    def get_profile(self, sensor_id: str) -> SensorProfile:
        """Return profile for sensor_id; fall back by prefix match, then default."""
        if sensor_id in self._sensor_profiles:
            return self._sensor_profiles[sensor_id]
        # prefix match: "radar-north" → "radar"
        for key in self._sensor_profiles:
            if sensor_id.startswith(key):
                return self._sensor_profiles[key]
        return SensorProfile()  # default: 100m, 5 m/s

    # ── Main update ────────────────────────────────────────────────────────

    def update(self, meas: SensorMeasurement) -> FusedTrack:
        """
        Incorporate one sensor measurement.

        1. Check hint map: if this sensor_id+track_hint has been seen before,
           reuse the same fused track.
        2. Otherwise run GNN: find nearest track within gate_m.
        3. If no match, create a new fused track (first measurement handled
           inside _create_track — no double-counting).
        4. For existing tracks: apply information-filter update.
        Returns the (updated) FusedTrack.
        """
        self._evict_stale()

        # 1. Hint-based lookup (fast path for multi-report streams)
        hint_key  = f"{meas.sensor_id}:{meas.track_hint}"
        fused_id  = self._hint_map.get(hint_key)
        fused     = self._tracks.get(fused_id) if fused_id else None  # type: ignore[arg-type]

        # 2. GNN association if no hint hit
        if fused is None:
            fused = self._gnn_associate(meas.lat, meas.lon)

        # Register hint → fused mapping (before possible creation so map is
        # always populated after this call)
        if fused is not None:
            self._hint_map[hint_key] = fused.id
            # 4. Existing track: information-filter update
            self._info_update(fused, meas)
        else:
            # 3. New track: _create_track handles the first measurement entirely
            fused = self._create_track(meas)
            self._tracks[fused.id] = fused
            self._hint_map[hint_key] = fused.id

        return fused

    # ── GNN association ────────────────────────────────────────────────────

    def _gnn_associate(self, lat: float, lon: float) -> Optional[FusedTrack]:
        """Find the nearest FusedTrack within gate_m; return None if empty."""
        best_d    = self.gate_m
        best_fused: Optional[FusedTrack] = None
        for fused in self._tracks.values():
            d = _distance_m(lat, lon, fused.lat, fused.lon)
            if d < best_d:
                best_d     = d
                best_fused = fused
        return best_fused

    # ── Track creation ─────────────────────────────────────────────────────

    def _create_track(self, meas: SensorMeasurement) -> FusedTrack:
        profile = self.get_profile(meas.sensor_id)
        fused_id = f"FT-{uuid.uuid4().hex[:8].upper()}"
        e, n, z  = _to_enu(meas.lat, meas.lon, meas.alt_m, meas.lat, meas.lon)

        # Initialise information-filter accumulators from first measurement
        inv_R_pos   = 1.0 / profile.pos_var
        inv_R_speed = 1.0 / profile.speed_var
        inv_R_hd    = 1.0 / profile.heading_var

        return FusedTrack(
            id=fused_id,
            lat=meas.lat,
            lon=meas.lon,
            alt_m=meas.alt_m,
            speed_mps=meas.speed_mps,
            heading_deg=meas.heading_deg,
            contributing_sensors=[meas.sensor_id],
            pos_std_m=profile.pos_std_m,
            speed_std_mps=profile.speed_std_mps,
            last_update=meas.timestamp,
            sensor_reports=[_report_dict(meas)],
            # info filter initialisation
            _info_x=e * inv_R_pos,
            _info_y=n * inv_R_pos,
            _info_z=z * inv_R_pos,
            _info_sp=meas.speed_mps   * inv_R_speed,
            _info_hd=meas.heading_deg * inv_R_hd,
            _inv_R_pos=inv_R_pos,
            _inv_R_speed=inv_R_speed,
            _inv_R_hd=inv_R_hd,
            _ref_lat=meas.lat,
            _ref_lon=meas.lon,
        )

    # ── Information-filter update ──────────────────────────────────────────

    def _info_update(self, fused: FusedTrack, meas: SensorMeasurement) -> None:
        """
        Add measurement z_i with covariance R_i to fused state.
        Information filter: accumulate inv_R and inv_R·z, then divide.
        """
        profile = self.get_profile(meas.sensor_id)
        inv_R_pos   = 1.0 / profile.pos_var
        inv_R_speed = 1.0 / profile.speed_var
        inv_R_hd    = 1.0 / profile.heading_var

        # Convert measurement to ENU relative to the fused track's reference
        e, n, z = _to_enu(meas.lat, meas.lon, meas.alt_m,
                           fused._ref_lat, fused._ref_lon)

        # Accumulate
        fused._info_x  += e * inv_R_pos
        fused._info_y  += n * inv_R_pos
        fused._info_z  += z * inv_R_pos
        fused._inv_R_pos   += inv_R_pos

        fused._info_sp     += meas.speed_mps   * inv_R_speed
        fused._inv_R_speed += inv_R_speed

        # Heading: use angular difference to avoid wrap-around artefacts
        # Accumulate around current estimate
        hd_diff = _angle_diff(meas.heading_deg, fused.heading_deg)
        fused._info_hd    += (fused.heading_deg + hd_diff) * inv_R_hd
        fused._inv_R_hd   += inv_R_hd

        # Compute fused state x̂ = P · (Σ R_i⁻¹ · z_i)
        fused_e = fused._info_x  / fused._inv_R_pos
        fused_n = fused._info_y  / fused._inv_R_pos
        fused_z = fused._info_z  / fused._inv_R_pos
        fused.speed_mps   = fused._info_sp / fused._inv_R_speed
        fused.heading_deg = (fused._info_hd / fused._inv_R_hd) % 360

        fused.lat, fused.lon, fused.alt_m = _from_enu(
            fused_e, fused_n, fused_z, fused._ref_lat, fused._ref_lon
        )

        # Fused covariance: P = (Σ R_i⁻¹)⁻¹  → std = √P
        fused.pos_std_m      = math.sqrt(1.0 / fused._inv_R_pos)
        fused.speed_std_mps  = math.sqrt(1.0 / fused._inv_R_speed)

        # Update contributor list
        if meas.sensor_id not in fused.contributing_sensors:
            fused.contributing_sensors.append(meas.sensor_id)

        fused.last_update = meas.timestamp
        fused.sensor_reports.append(_report_dict(meas))
        if len(fused.sensor_reports) > 50:
            fused.sensor_reports = fused.sensor_reports[-50:]

    # ── Stale eviction ─────────────────────────────────────────────────────

    def _evict_stale(self) -> List[str]:
        """Remove tracks not updated within stale_s. Returns evicted IDs."""
        evicted: List[str] = []
        for fid in list(self._tracks.keys()):
            t = self._tracks[fid]
            # Use last_update timestamp if available; otherwise evict after 1 cycle
            try:
                from datetime import datetime, timezone
                lu = datetime.fromisoformat(t.last_update.replace("Z", "+00:00"))
                age_s = (datetime.now(timezone.utc) - lu).total_seconds()
                if age_s > self.stale_s:
                    evicted.append(fid)
            except Exception:
                pass
        for fid in evicted:
            del self._tracks[fid]
            # Clean hint map entries pointing to this track
            for k in [k for k, v in self._hint_map.items() if v == fid]:
                del self._hint_map[k]
        return evicted

    # ── Public accessors ───────────────────────────────────────────────────

    def get_track(self, fused_id: str) -> Optional[FusedTrack]:
        return self._tracks.get(fused_id)

    def all_tracks(self) -> List[FusedTrack]:
        return list(self._tracks.values())

    def track_count(self) -> int:
        return len(self._tracks)

    def stats(self) -> Dict[str, Any]:
        return {
            "fused_tracks":    self.track_count(),
            "sensor_profiles": list(self._sensor_profiles.keys()),
            "gate_m":          self.gate_m,
            "stale_s":         self.stale_s,
        }

    def reset(self) -> None:
        self._tracks.clear()
        self._hint_map.clear()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _report_dict(meas: SensorMeasurement) -> Dict[str, Any]:
    return {
        "sensor_id":   meas.sensor_id,
        "track_hint":  meas.track_hint,
        "lat":         round(meas.lat, 7),
        "lon":         round(meas.lon, 7),
        "alt_m":       round(meas.alt_m, 1),
        "speed_mps":   round(meas.speed_mps,   2),
        "heading_deg": round(meas.heading_deg, 1),
        "timestamp":   meas.timestamp,
    }


# ── Singleton for server.py integration ──────────────────────────────────────

# Server.py imports this and calls engine.update() inside ingest handler.
engine = FusionEngine()
