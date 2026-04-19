"""Geofence tests — no-fly zone enforcement."""
from __future__ import annotations

from services.autonomy.geofence import NoFlyZone, haversine_m, violates_geofence
from services.autonomy.schemas import Waypoint


ANKARA_CENTER = NoFlyZone(
    zone_id="NFZ-ANK-001",
    name="Ankara Sivil Havalimanı 5km",
    center_lat=39.9512, center_lon=32.9968, radius_m=5000,
)


def test_haversine_same_point_zero():
    assert haversine_m(39.9, 32.8, 39.9, 32.8) < 0.01


def test_haversine_approx_1km_north():
    # ~1 km kuzey
    d = haversine_m(39.9334, 32.8597, 39.9424, 32.8597)
    assert 900 < d < 1100


def test_waypoint_outside_zone_ok():
    wp = Waypoint(latitude=39.8, longitude=32.7, altitude_m=100)  # çok uzakta
    assert violates_geofence(wp, [ANKARA_CENTER]) is None


def test_waypoint_inside_zone_flagged():
    wp = Waypoint(latitude=39.9512, longitude=32.9968, altitude_m=100)  # merkez
    result = violates_geofence(wp, [ANKARA_CENTER])
    assert result is not None
    assert result.zone_id == "NFZ-ANK-001"


def test_above_ceiling_allowed():
    ceiling_zone = NoFlyZone(
        zone_id="NFZ-CIVIL", name="sivil alan",
        center_lat=39.9, center_lon=32.8, radius_m=5000, ceiling_m=500,
    )
    # Zone içinde ama tavandan yüksek → izinli
    wp = Waypoint(latitude=39.9, longitude=32.8, altitude_m=1000)
    assert violates_geofence(wp, [ceiling_zone]) is None


def test_below_ceiling_inside_blocked():
    ceiling_zone = NoFlyZone(
        zone_id="NFZ", name="x",
        center_lat=39.9, center_lon=32.8, radius_m=5000, ceiling_m=500,
    )
    wp = Waypoint(latitude=39.9, longitude=32.8, altitude_m=200)
    assert violates_geofence(wp, [ceiling_zone]) is not None


def test_empty_zones_always_ok():
    wp = Waypoint(latitude=0.0, longitude=0.0)
    assert violates_geofence(wp, []) is None
