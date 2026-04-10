"""
adapters/sensor_adapter_base.py  —  Sensor-agnostic adapter base for NIZAM

All physical-sensor adapters (ADS-B, AIS, CoT, MQTT, REST, radar, IFF …)
share the same contract:

  1. Produce track.update events in the NIZAM canonical format.
  2. Tag every event with a sensor_id so the fusion engine can weight it.
  3. Respect an optional rate limiter to avoid overwhelming the ingest bus.
  4. Output either to stdout (pipe-based) or HTTP POST (--cop_url mode).

This module provides:
  SensorAdapterBase   — abstract base class with shared utilities
  SensorRegistry      — maps sensor_id → SensorProfile for the local process
  NormalizedTrack     — validated dataclass for the canonical payload fields
  make_nizam_event()  — build the NIZAM envelope around a NormalizedTrack

Concrete adapters (adsb_adapter.py, cot_adapter.py, …) only need to:
  1. Subclass SensorAdapterBase  (or use make_nizam_event() directly).
  2. Implement run() / fetch_and_emit() for their specific protocol.

Fusion integration
------------------
When the COP server has fusion enabled, each ingested track.update carries
  payload.supporting_sensors  — list of sensor IDs
The fusion engine uses this to look up the SensorProfile and apply the
appropriate measurement noise covariance.

Usage example
-------------
    from adapters.sensor_adapter_base import make_nizam_event, NormalizedTrack
    import uuid

    track = NormalizedTrack(
        track_id="RADAR-001",
        sensor_id="radar-north",
        lat=41.015, lon=28.979,
        alt_m=3000, speed_mps=250, heading_deg=90,
        classification="aircraft",
        threat_level="LOW",
    )
    ev = make_nizam_event(track)
    print(json.dumps(ev))
"""
from __future__ import annotations

import abc
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    from shared.utils import utc_now_iso
except ImportError:
    from datetime import datetime, timezone
    def utc_now_iso() -> str:  # type: ignore[misc]
        return datetime.now(timezone.utc).isoformat()


# ── Canonical track ────────────────────────────────────────────────────────────

@dataclass
class NormalizedTrack:
    """
    Sensor-agnostic canonical representation of a single track report.

    All adapters must populate at minimum: track_id, sensor_id, lat, lon.
    Remaining fields have sane defaults.
    """
    # Identity
    track_id:        str
    sensor_id:       str            # e.g. "radar-north", "adsb", "cot"

    # Position
    lat:             float
    lon:             float
    alt_m:           float          = 0.0

    # Kinematics
    speed_mps:       float          = 0.0
    heading_deg:     float          = 0.0
    vertical_rate_mps: float        = 0.0

    # Classification
    classification:  str            = "unknown"   # aircraft / ground_vehicle / …
    class_confidence: float         = 0.5
    callsign:        str            = ""

    # Threat / intent
    threat_level:    str            = "LOW"       # LOW / MEDIUM / HIGH
    threat_score:    float          = 0.1
    intent:          str            = "unknown"
    intent_conf:     float          = 0.5

    # Provenance
    timestamp:       str            = field(default_factory=utc_now_iso)
    raw_metadata:    Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """Raise ValueError if required fields are missing or out of range."""
        if not self.track_id:
            raise ValueError("track_id is required")
        if not self.sensor_id:
            raise ValueError("sensor_id is required")
        if not (-90 <= self.lat <= 90):
            raise ValueError(f"lat out of range: {self.lat}")
        if not (-180 <= self.lon <= 180):
            raise ValueError(f"lon out of range: {self.lon}")
        if self.threat_level not in ("LOW", "MEDIUM", "HIGH"):
            raise ValueError(f"invalid threat_level: {self.threat_level}")


# ── Event factory ─────────────────────────────────────────────────────────────

def make_nizam_event(
    track: NormalizedTrack,
    history: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Build a complete NIZAM track.update event envelope from a NormalizedTrack.

    This is the canonical factory used by all adapters so the payload schema
    stays consistent and the fusion engine can rely on supporting_sensors.
    """
    return {
        "schema_version": "1.1",
        "event_id":       str(uuid.uuid4()),
        "event_type":     "track.update",
        "timestamp":      track.timestamp,
        "source": {
            "agent_id":    f"{track.sensor_id}-adapter",
            "instance_id": track.sensor_id,
            "host":        "local",
        },
        "correlation_id": track.track_id,
        "payload": {
            "global_track_id":   track.track_id,
            "id":                track.track_id,
            "lat":               round(track.lat, 7),
            "lon":               round(track.lon, 7),
            "status":            "CONFIRMED",
            "classification": {
                "label":      track.classification,
                "confidence": round(track.class_confidence, 3),
                "callsign":   track.callsign,
            },
            "supporting_sensors": [track.sensor_id],
            "kinematics": {
                "speed_mps":          round(track.speed_mps,          2),
                "heading_deg":        round(track.heading_deg,         1),
                "altitude_m":         round(track.alt_m,               1),
                "vertical_rate_mps":  round(track.vertical_rate_mps,   2),
            },
            "intent":       track.intent,
            "intent_conf":  round(track.intent_conf, 3),
            "threat_level": track.threat_level,
            "threat_score": round(track.threat_score, 3),
            "history":      history or [],
            "_sensor_meta": track.raw_metadata,
        },
    }


# ── Per-process sensor registry ───────────────────────────────────────────────

class SensorRegistry:
    """
    Lightweight in-process registry mapping sensor_id → metadata dict.
    Adapters register themselves at startup; server.py can query the
    registry to pre-populate the fusion engine's sensor profiles.
    """

    def __init__(self) -> None:
        self._sensors: Dict[str, Dict[str, Any]] = {}

    def register(
        self,
        sensor_id:    str,
        sensor_type:  str  = "unknown",
        pos_std_m:    float = 100.0,
        speed_std_mps: float = 5.0,
        description:  str  = "",
    ) -> None:
        self._sensors[sensor_id] = {
            "sensor_id":    sensor_id,
            "sensor_type":  sensor_type,
            "pos_std_m":    pos_std_m,
            "speed_std_mps": speed_std_mps,
            "description":  description,
            "registered_at": utc_now_iso(),
        }

    def get(self, sensor_id: str) -> Optional[Dict[str, Any]]:
        return self._sensors.get(sensor_id)

    def all(self) -> List[Dict[str, Any]]:
        return list(self._sensors.values())


# Module-level singleton used by adapters that run in the same process as COP
registry = SensorRegistry()


# ── Rate limiter ───────────────────────────────────────────────────────────────

class AdapterRateLimiter:
    """
    Token-bucket rate limiter per track ID.
    Allows at most rate_hz updates per second per track.
    0 = unlimited.
    """

    def __init__(self, rate_hz: float = 0.0) -> None:
        self.rate_hz    = rate_hz
        self._last: Dict[str, float] = {}

    def allow(self, track_id: str) -> bool:
        if self.rate_hz <= 0:
            return True
        now      = time.monotonic()
        interval = 1.0 / self.rate_hz
        last     = self._last.get(track_id, 0.0)
        if now - last >= interval:
            self._last[track_id] = now
            return True
        return False


# ── Output handler ─────────────────────────────────────────────────────────────

class AdapterOutput:
    """
    Emit NIZAM events to stdout (pipe mode) or HTTP POST (direct mode).
    All adapters should use this so output behaviour is consistent.
    """

    def __init__(
        self,
        cop_url:  Optional[str] = None,
        api_key:  Optional[str] = None,
        timeout_s: float        = 5.0,
    ) -> None:
        self.cop_url   = cop_url.rstrip("/") if cop_url else None
        self.api_key   = api_key
        self.timeout_s = timeout_s
        self._errors   = 0

    def emit(self, ev: Dict[str, Any]) -> bool:
        """Emit one event. Returns True on success."""
        line = json.dumps(ev, ensure_ascii=False)
        if not self.cop_url:
            print(line, flush=True)
            return True
        return self._http_post(line.encode())

    def _http_post(self, data: bytes) -> bool:
        import urllib.request
        req = urllib.request.Request(
            f"{self.cop_url}/api/ingest",
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent":   "NIZAM-sensor-adapter/1.0",
                **({"X-API-Key": self.api_key} if self.api_key else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s):
                self._errors = 0
                return True
        except Exception as exc:
            self._errors += 1
            print(f"[adapter] HTTP error ({self._errors}): {exc}", file=sys.stderr)
            return False

    @property
    def error_count(self) -> int:
        return self._errors


# ── Abstract base ─────────────────────────────────────────────────────────────

class SensorAdapterBase(abc.ABC):
    """
    Abstract base for all NIZAM sensor adapters.

    Subclasses implement:
      fetch()   — one poll cycle, returns list[NormalizedTrack]
      run()     — main loop (default impl calls fetch() in a loop)

    Subclasses optionally override:
      sensor_id   — unique ID for this adapter instance
      sensor_type — type string (radar/adsb/cot/mqtt/…)
    """

    sensor_id:   str = "unknown-sensor"
    sensor_type: str = "unknown"

    def __init__(
        self,
        cop_url:    Optional[str]  = None,
        api_key:    Optional[str]  = None,
        rate_hz:    float          = 0.0,
        interval_s: float          = 1.0,
    ) -> None:
        self.output      = AdapterOutput(cop_url, api_key)
        self.rate_limiter = AdapterRateLimiter(rate_hz)
        self.interval_s  = interval_s
        registry.register(
            sensor_id=self.sensor_id,
            sensor_type=self.sensor_type,
        )

    @abc.abstractmethod
    def fetch(self) -> List[NormalizedTrack]:
        """Poll the sensor and return a list of normalised track reports."""

    def emit_track(self, track: NormalizedTrack) -> None:
        """Validate, rate-limit, and emit one track."""
        try:
            track.validate()
        except ValueError as exc:
            print(f"[{self.sensor_id}] invalid track: {exc}", file=sys.stderr)
            return
        if not self.rate_limiter.allow(track.track_id):
            return
        ev = make_nizam_event(track)
        self.output.emit(ev)

    def run(self, max_cycles: int = 0) -> None:
        """
        Main poll loop.  Runs forever (or max_cycles times if > 0).
        Override for push-based sources (WebSocket, MQTT, UDP, …).
        """
        cycle = 0
        print(f"[{self.sensor_id}] running (interval={self.interval_s}s)", file=sys.stderr)
        try:
            while True:
                tracks = self.fetch()
                for track in tracks:
                    self.emit_track(track)
                cycle += 1
                if max_cycles and cycle >= max_cycles:
                    break
                time.sleep(self.interval_s)
        except KeyboardInterrupt:
            pass
        print(f"[{self.sensor_id}] stopped after {cycle} cycles", file=sys.stderr)
