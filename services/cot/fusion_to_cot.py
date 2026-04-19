"""Füzyon Track → CoT dönüşümü.

Track state + confidence → CoT type eşlemesi:
  confirmed + high-conf  → hostile UAV (a-h-A-M-F-U)
  confirmed + low-conf   → unknown UAV
  tentative / lost       → unknown UAV
"""
from __future__ import annotations

import math
from datetime import datetime
from xml.etree import ElementTree as ET

from services.cot.cot_builder import (
    COT_TYPE_HOSTILE_UAV,
    COT_TYPE_UNKNOWN_UAV,
    build_cot_event,
)

# Dünya yarıçapı (WGS84 equatorial)
_EARTH_RADIUS_M = 6378137.0

# High-confidence eşiği — bunun altındaki track'ler "unknown" kalır
DEFAULT_HOSTILE_THRESHOLD = 0.75


def enu_to_latlon(
    east_m: float, north_m: float, ref_lat: float, ref_lon: float
) -> tuple[float, float]:
    """Küçük sahada (~10 km) yerel düzlem ENU → lat/lon dönüşümü.

    Düz-Earth yaklaşımı; füzyon ENU'dan CoT'ye çevirmek için yeterli.
    """
    d_lat_rad = north_m / _EARTH_RADIUS_M
    d_lon_rad = east_m / (_EARTH_RADIUS_M * math.cos(math.radians(ref_lat)))
    return (ref_lat + math.degrees(d_lat_rad), ref_lon + math.degrees(d_lon_rad))


def track_cot_type(
    state: str, confidence: float, hostile_threshold: float = DEFAULT_HOSTILE_THRESHOLD
) -> str:
    if state == "confirmed" and confidence >= hostile_threshold:
        return COT_TYPE_HOSTILE_UAV
    return COT_TYPE_UNKNOWN_UAV


def track_to_cot(
    track: dict,
    ref_lat: float,
    ref_lon: float,
    hostile_threshold: float = DEFAULT_HOSTILE_THRESHOLD,
    stale_sec: int = 30,
    clock_now: datetime | None = None,
) -> ET.Element:
    """Track (pydantic dump) → CoT event."""
    if "latitude" in track and "longitude" in track:
        lat, lon = float(track["latitude"]), float(track["longitude"])
    else:
        lat, lon = enu_to_latlon(track["x"], track["y"], ref_lat, ref_lon)

    alt_hae = float(track.get("altitude", track.get("z", 0.0)))
    cot_type = track_cot_type(track["state"], float(track.get("confidence", 0.5)), hostile_threshold)

    vx = float(track.get("vx", 0.0))
    vy = float(track.get("vy", 0.0))
    speed = math.sqrt(vx * vx + vy * vy)
    course = (math.degrees(math.atan2(vx, vy)) + 360.0) % 360.0 if speed > 0.1 else None

    callsign = track.get("uas_id") or track.get("class_name") or f"TRK-{track['track_id'][:6]}"
    hits = int(track.get("hits", 0))
    sources = track.get("sources", [])
    remarks = (
        f"hits={hits} conf={track.get('confidence', 0.0):.2f} "
        f"state={track['state']} sources={','.join(sources)}"
    )

    return build_cot_event(
        uid=f"NIZAM.{track['track_id']}",
        cot_type=cot_type,
        latitude=lat,
        longitude=lon,
        altitude_hae_m=alt_hae,
        course_deg=course,
        speed_mps=speed if speed > 0 else None,
        callsign=callsign,
        remarks=remarks,
        stale_sec=stale_sec,
        clock_now=clock_now,
    )
