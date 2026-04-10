"""
cop/weather.py — Weather Observation Service

Provides simulated METAR-style weather observations for the operational area.
In production, replace _fetch_live() with a real METAR/TAF data source
(e.g. aviationweather.gov API, OpenWeatherMap, or an internal NWS feed).

Each observation dict:
  station    : ICAO station identifier
  name       : human-readable name
  lat, lon   : position
  temp_c     : temperature (°C)
  dew_c      : dew point (°C)
  wind_dir   : wind direction (degrees true)
  wind_kt    : wind speed (knots)
  gust_kt    : gust speed (knots, None if calm)
  visibility_m : prevailing visibility (metres)
  ceiling_ft : cloud ceiling (feet AGL, None if CAVOK)
  wx         : present weather string ("RA", "TSRA", "FG", "SN", "", …)
  metar      : full METAR string (synthetic)
  updated_at : ISO-8601 timestamp

Tactical implications surfaced to operators:
  • Low visibility (< 3000 m) → reduced sensor range warning
  • Strong cross-wind (> 20 kt) → effector accuracy degraded
  • Thunderstorm / precipitation → RF attenuation warning
"""
from __future__ import annotations

import math
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ── Station database (Turkey + surrounding AO) ────────────────────────────────

_STATIONS: List[Dict[str, Any]] = [
    {"id": "LTBA", "name": "Istanbul Atatürk",    "lat": 40.976, "lon": 28.814},
    {"id": "LTFM", "name": "Istanbul Sabiha",     "lat": 40.898, "lon": 29.309},
    {"id": "LTAC", "name": "Ankara Esenboğa",     "lat": 40.128, "lon": 32.995},
    {"id": "LTAI", "name": "Antalya",              "lat": 36.899, "lon": 30.800},
    {"id": "LTBS", "name": "Bodrum Milas",        "lat": 37.250, "lon": 27.664},
    {"id": "LTBJ", "name": "İzmir Adnan",         "lat": 38.292, "lon": 27.157},
    {"id": "LTFE", "name": "Şanlıurfa GAP",       "lat": 37.445, "lon": 38.846},
    {"id": "LTCG", "name": "Trabzon",              "lat": 40.995, "lon": 39.789},
]

# ── Simulation state ──────────────────────────────────────────────────────────

_obs_cache: Dict[str, Dict[str, Any]] = {}
_last_refresh: float = 0.0
_CACHE_TTL_S = 300.0   # simulate new obs every 5 minutes

# ── Random seed for repeatable weather patterns ───────────────────────────────

_rng = random.Random(42)


# ── Public API ────────────────────────────────────────────────────────────────

def get_observations(force_refresh: bool = False) -> List[Dict[str, Any]]:
    """Return current weather observations for all stations."""
    global _last_refresh
    now = time.time()
    if force_refresh or (now - _last_refresh) > _CACHE_TTL_S:
        _refresh()
        _last_refresh = now
    return list(_obs_cache.values())


def get_station(station_id: str) -> Optional[Dict[str, Any]]:
    """Return observation for a specific station."""
    get_observations()
    return _obs_cache.get(station_id)


def tactical_warnings(observations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Return tactical weather warnings derived from observations.
    Each warning: {station, type, severity, message}
    """
    warnings = []
    for obs in observations:
        sid   = obs["station"]
        name  = obs["name"]
        vis   = obs.get("visibility_m", 9999)
        wind  = obs.get("wind_kt", 0)
        gust  = obs.get("gust_kt") or wind
        wx    = obs.get("wx", "")

        if vis < 1000:
            warnings.append({
                "station": sid, "name": name, "type": "LOW_VISIBILITY",
                "severity": "HIGH",
                "message": f"{name}: görüş {vis}m — sensör menzili kritik azalma",
            })
        elif vis < 3000:
            warnings.append({
                "station": sid, "name": name, "type": "REDUCED_VISIBILITY",
                "severity": "MEDIUM",
                "message": f"{name}: görüş {vis}m — azalmış optik algılama",
            })
        if gust > 30:
            warnings.append({
                "station": sid, "name": name, "type": "HIGH_WIND",
                "severity": "HIGH",
                "message": f"{name}: rüzgar {wind}kt gustr {gust}kt — effektör isabeti bozulmuş",
            })
        elif gust > 20:
            warnings.append({
                "station": sid, "name": name, "type": "STRONG_WIND",
                "severity": "MEDIUM",
                "message": f"{name}: rüzgar {wind}kt — effektör etkinliği azalmış",
            })
        if "TS" in wx:
            warnings.append({
                "station": sid, "name": name, "type": "THUNDERSTORM",
                "severity": "HIGH",
                "message": f"{name}: gök gürültülü fırtına — RF zayıflama yüksek",
            })
        elif wx in ("RA", "SN", "-RA", "+RA"):
            warnings.append({
                "station": sid, "name": name, "type": "PRECIPITATION",
                "severity": "LOW",
                "message": f"{name}: çökme ({wx}) — RF zayıflama orta",
            })
    return warnings


# ── Simulation engine ─────────────────────────────────────────────────────────

def _refresh() -> None:
    """Generate a fresh set of simulated weather observations."""
    ts = datetime.now(timezone.utc).strftime("%d%H%MZ")
    for st in _STATIONS:
        sid = st["id"]
        prev = _obs_cache.get(sid, {})
        obs  = _simulate_obs(st, prev, ts)
        _obs_cache[sid] = obs


def _simulate_obs(station: Dict, prev: Dict, ts: str) -> Dict[str, Any]:
    """Generate a plausible weather observation with gentle random walk."""
    sid  = station["id"]
    lat  = station["lat"]
    lon  = station["lon"]

    # Temperature: coastal ~15-22°C, inland more variable
    base_temp = 18.0 if abs(lat - 37) < 4 else 12.0
    temp = _walk(prev.get("temp_c", base_temp), base_temp, 1.5, -10, 45)
    dew  = min(temp - 2, _walk(prev.get("dew_c", temp - 8), temp - 8, 1.0, -20, 30))

    # Wind
    wind_dir = int(_walk(prev.get("wind_dir", 270), 270, 20, 0, 359)) % 360
    wind_kt  = max(0, int(_walk(prev.get("wind_kt", 8), 8, 3, 0, 60)))
    gust_kt  = (wind_kt + _rng.randint(3, 12)) if wind_kt > 10 else None

    # Visibility (mostly CAVOK, occasionally reduced)
    vis_base  = prev.get("visibility_m", 9999)
    wx_roll   = _rng.random()
    if wx_roll < 0.04:
        vis, wx = _rng.choice([500, 800, 1200]), _rng.choice(["FG", "MIFG"])
    elif wx_roll < 0.12:
        vis, wx = _rng.randint(2000, 4000), _rng.choice(["-RA", "RA", "HZ"])
    elif wx_roll < 0.16:
        vis, wx = _rng.randint(1500, 3000), "TSRA"
    else:
        vis, wx = 9999, ""

    # Ceiling
    ceiling_ft = None
    if wx:
        ceiling_ft = _rng.choice([800, 1200, 2500, 3500])

    # Build synthetic METAR
    vis_str = f"{vis:04d}" if vis < 9999 else "9999"
    cloud   = f"BKN{ceiling_ft // 100:03d}" if ceiling_ft else "CAVOK"
    wx_str  = wx + " " if wx else ""
    metar = (
        f"METAR {sid} {ts} "
        f"{wind_dir:03d}{wind_kt:02d}{'G'+str(gust_kt) if gust_kt else ''}KT "
        f"{vis_str} {wx_str}{cloud} "
        f"{int(temp):+03d}/{int(dew):+03d} Q{_qnh(temp, lat)}"
    )

    return {
        "station":      sid,
        "name":         station["name"],
        "lat":          lat,
        "lon":          lon,
        "temp_c":       round(temp, 1),
        "dew_c":        round(dew, 1),
        "wind_dir":     wind_dir,
        "wind_kt":      wind_kt,
        "gust_kt":      gust_kt,
        "visibility_m": vis,
        "ceiling_ft":   ceiling_ft,
        "wx":           wx,
        "metar":        metar,
        "updated_at":   datetime.now(timezone.utc).isoformat(),
    }


def _walk(current: float, mean: float, step: float, lo: float, hi: float) -> float:
    """Mean-reverting random walk clamped to [lo, hi]."""
    drift = (mean - current) * 0.15
    val   = current + drift + _rng.gauss(0, step)
    return max(lo, min(hi, val))


def _qnh(temp: float, lat: float) -> int:
    """Simulate a plausible QNH based on temperature and latitude."""
    base = 1013 + int((temp - 15) * 0.4) - int(abs(lat - 40) * 0.3)
    return max(970, min(1040, base + _rng.randint(-3, 3)))
