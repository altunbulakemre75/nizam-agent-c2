"""Coğrafi dönüşümler — pyproj kurulu ise tam ETRS89/WGS84 ENU.
Yoksa küçük-saha düz-Earth fallback (~%0.01 hata / 10 km).

Düz-Earth sadece tek sitede ve küçük yarıçapta (<20km) doğrudur. Multi-site
veya yüksek doğruluk için pyproj şart.
"""
from __future__ import annotations

import math
from functools import lru_cache

_EARTH_R_M = 6378137.0


try:
    from pyproj import Transformer, CRS   # noqa: PLC0415
    _HAS_PYPROJ = True
except ImportError:
    _HAS_PYPROJ = False


@lru_cache(maxsize=32)
def _enu_transformer(ref_lat: float, ref_lon: float, ref_alt: float = 0.0):
    """pyproj local-tangent-plane transformer.

    `+proj=ortho` orthographic projection: küçük sahada (~100 km yarıçap)
    east/north'a eşdeğer, proj 7+ ile evrensel destek. +proj=topocentric
    proj 9.x'te var ama tüm sistemlerde mevcut değil; ortho taşınabilir.
    """
    # ref_alt şu anki ortho projeksiyonunda kullanılmıyor; alt farkı
    # u = alt - ref_alt ile düz çıkartılıyor. (topocentric proj 9.x
    # geldiğinde buraya inline edilecek.)
    del ref_alt
    if not _HAS_PYPROJ:
        return None
    local = CRS.from_proj4(
        f"+proj=ortho +lat_0={ref_lat} +lon_0={ref_lon} +ellps=WGS84 +units=m"
    )
    wgs84 = CRS.from_epsg(4326)
    return Transformer.from_crs(wgs84, local, always_xy=True)


def latlon_to_enu(
    lat: float, lon: float, ref_lat: float, ref_lon: float,
    alt: float = 0.0, ref_alt: float = 0.0,
) -> tuple[float, float, float]:
    """Lat/lon → ENU (east, north, up) metre.

    pyproj varsa kullanır (her ölçek doğru); yoksa düz-Earth fallback.
    """
    transformer = _enu_transformer(ref_lat, ref_lon, ref_alt)
    if transformer is not None:
        try:
            e, n = transformer.transform(lon, lat)
            return float(e), float(n), alt - ref_alt
        except Exception:
            pass   # düz-Earth fallback

    d_lat = math.radians(lat - ref_lat)
    d_lon = math.radians(lon - ref_lon)
    east = d_lon * _EARTH_R_M * math.cos(math.radians(ref_lat))
    north = d_lat * _EARTH_R_M
    up = alt - ref_alt
    return east, north, up


def enu_to_latlon(
    east: float, north: float, ref_lat: float, ref_lon: float,
    up: float = 0.0, ref_alt: float = 0.0,
) -> tuple[float, float, float]:
    """ENU → lat/lon. Geri çevirim."""
    transformer = _enu_transformer(ref_lat, ref_lon, ref_alt)
    if transformer is not None:
        try:
            lon, lat = transformer.transform(east, north, direction="INVERSE")
            return float(lat), float(lon), up + ref_alt
        except Exception:
            pass

    d_lat = math.degrees(north / _EARTH_R_M)
    d_lon = math.degrees(east / (_EARTH_R_M * math.cos(math.radians(ref_lat))))
    return ref_lat + d_lat, ref_lon + d_lon, up + ref_alt


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


def has_pyproj() -> bool:
    return _HAS_PYPROJ
