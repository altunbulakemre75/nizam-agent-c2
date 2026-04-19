"""Intercept planlayıcı — track konumundan ileri tahminli buluşma noktası.

Geofence ihlali + operatör onayı eksikliği intercept'i engeller.
Hedefin mevcut hızını kullanarak lookahead_s saniye sonraki konum
tahmin edilir ve oraya approach waypoint'i üretilir.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from services.autonomy.geofence import NoFlyZone, violates_geofence
from services.autonomy.schemas import InterceptCommand, InterceptPhase, Waypoint

_EARTH_R_M = 6378137.0


class InterceptRefused(Exception):
    """Intercept planı güvenlik nedeniyle üretilemedi."""


def _offset_latlon(
    lat: float, lon: float, east_m: float, north_m: float
) -> tuple[float, float]:
    d_lat = math.degrees(north_m / _EARTH_R_M)
    d_lon = math.degrees(east_m / (_EARTH_R_M * math.cos(math.radians(lat))))
    return lat + d_lat, lon + d_lon


def predict_target_position(
    lat: float, lon: float, alt_m: float,
    vx_mps: float, vy_mps: float, vz_mps: float,
    lookahead_s: float,
) -> Waypoint:
    """Sabit-hız lookahead. vx=east, vy=north (ENU)."""
    east = vx_mps * lookahead_s
    north = vy_mps * lookahead_s
    new_lat, new_lon = _offset_latlon(lat, lon, east, north)
    new_alt = max(0.0, alt_m + vz_mps * lookahead_s)
    return Waypoint(latitude=new_lat, longitude=new_lon, altitude_m=new_alt)


def plan_intercept(
    track: dict,
    *,
    operator_approved: bool,
    approved_by: str | None = None,
    no_fly_zones: list[NoFlyZone] | None = None,
    lookahead_s: float = 5.0,
    max_approach_m: float = 100.0,
) -> InterceptCommand:
    """Track'tan InterceptCommand üret.

    Raises:
        InterceptRefused: operatör onayı yoksa veya geofence ihlal ederse
    """
    if not operator_approved:
        raise InterceptRefused("operator_approved=False — intercept reddedildi")

    if "latitude" not in track or "longitude" not in track:
        raise InterceptRefused("track'te lat/lon yok — ENU konumdan planlama desteklenmiyor")

    vx = float(track.get("vx", 0.0))
    vy = float(track.get("vy", 0.0))
    vz = float(track.get("vz", 0.0))
    alt = float(track.get("altitude", track.get("z", 100.0)))

    wp = predict_target_position(
        track["latitude"], track["longitude"], alt, vx, vy, vz, lookahead_s,
    )

    zones = no_fly_zones or []
    violated = violates_geofence(wp, zones)
    if violated is not None:
        raise InterceptRefused(
            f"waypoint {violated.zone_id} ({violated.name}) no-fly zone'unu ihlal ediyor"
        )

    return InterceptCommand(
        target_track_id=track["track_id"],
        phase=InterceptPhase.APPROACH,
        waypoint=wp,
        max_approach_distance_m=max_approach_m,
        operator_approved=True,
        approved_by=approved_by,
        approved_at_iso=datetime.now(timezone.utc).isoformat(),
    )
