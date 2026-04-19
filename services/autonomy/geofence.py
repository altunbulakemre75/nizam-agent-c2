"""Geofence — dost bölgeleri ve sivil alanları ihlal engelleme.

Her intercept komutu waypoint'inin yasaklı bölgelerle kesişmediği
doğrulanır. Yasak bölge = no-fly zone (sivil, dost üs, havalanı...).
"""
from __future__ import annotations

import math

from pydantic import BaseModel

from services.autonomy.schemas import Waypoint


class NoFlyZone(BaseModel):
    """Dairesel no-fly zone (lat/lon merkez + yarıçap)."""
    zone_id: str
    name: str
    center_lat: float
    center_lon: float
    radius_m: float
    ceiling_m: float | None = None  # None = tüm irtifalar


_EARTH_R_M = 6378137.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """İki nokta arası büyük-daire mesafesi (metre)."""
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return 2 * _EARTH_R_M * math.asin(math.sqrt(max(0.0, a)))


def violates_geofence(wp: Waypoint, zones: list[NoFlyZone]) -> NoFlyZone | None:
    """Waypoint herhangi bir no-fly zone'a giriyorsa ilk eşleşen zone'u döner."""
    for zone in zones:
        dist = haversine_m(wp.latitude, wp.longitude, zone.center_lat, zone.center_lon)
        if dist > zone.radius_m:
            continue
        if zone.ceiling_m is not None and wp.altitude_m > zone.ceiling_m:
            continue
        return zone
    return None
