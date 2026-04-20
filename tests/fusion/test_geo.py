"""shared.geo tests — pyproj varsa tam, yoksa düz-Earth."""
from __future__ import annotations

import pytest

from shared.geo import enu_to_latlon, has_pyproj, haversine_m, latlon_to_enu


ANKARA = (39.9334, 32.8597)


def test_origin_is_zero():
    e, n, u = latlon_to_enu(*ANKARA, *ANKARA)
    assert abs(e) < 1e-3
    assert abs(n) < 1e-3
    assert abs(u) < 1e-3


def test_roundtrip_small_distance():
    # 1 km kuzey
    target_lat = ANKARA[0] + 0.009   # ~1 km
    target_lon = ANKARA[1]
    e, n, u = latlon_to_enu(target_lat, target_lon, *ANKARA)
    lat2, lon2, _ = enu_to_latlon(e, n, *ANKARA, up=u)
    assert abs(lat2 - target_lat) < 1e-6
    assert abs(lon2 - target_lon) < 1e-6


def test_roundtrip_mid_distance():
    # 50 km kuzeydoğu
    target_lat = ANKARA[0] + 0.3
    target_lon = ANKARA[1] + 0.4
    e, n, _ = latlon_to_enu(target_lat, target_lon, *ANKARA)
    lat2, lon2, _ = enu_to_latlon(e, n, *ANKARA)
    # pyproj varsa cm doğruluk, yoksa düz-Earth ~100m hata kabul edilebilir
    tol = 1e-5 if has_pyproj() else 2e-3
    assert abs(lat2 - target_lat) < tol
    assert abs(lon2 - target_lon) < tol


def test_haversine_zero_at_same_point():
    assert haversine_m(39.9, 32.8, 39.9, 32.8) < 0.01


def test_haversine_1km_north():
    d = haversine_m(39.9334, 32.8597, 39.9424, 32.8597)   # ~1 km
    assert 900 < d < 1100


def test_up_dimension_preserved():
    _, _, u = latlon_to_enu(*ANKARA, *ANKARA, alt=500.0, ref_alt=100.0)
    assert abs(u - 400.0) < 1.0
