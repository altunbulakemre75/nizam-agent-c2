"""
adsb_adapter.py  —  ADS-B real sensor adapter for NIZAM

Reads live aircraft positions from:
  --source dump1090      : local dump1090 / readsb JSON API (http://localhost:8080/aircraft.json)
  --source opensky       : OpenSky Network public REST API (no auth, rate-limited ~100 req/day)
  --source adsbfi        : api.adsb.fi  — free, no auth, generous rate limits
  --source airplaneslive : api.airplanes.live/v2 — free, no auth
  --source file          : JSONL file with dump1090-format aircraft records (for testing)

Output modes:
  stdout (default)        : JSONL → pipe into cop_publisher.py
  --cop_url http://...    : POST directly to COP /api/ingest (no pipe needed)

Usage:
  # dump1090 (RTL-SDR plugged in, readsb/dump1090 running)
  python adapters/adsb_adapter.py --source dump1090 | python agents/cop_publisher.py

  # OpenSky public API (bounding box around Istanbul)
  python adapters/adsb_adapter.py --source opensky --lat 41.0 --lon 29.0 --radius_km 150

  # ADSB.fi — direct to COP (no pipe needed)
  python adapters/adsb_adapter.py --source adsbfi --cop_url http://localhost:8100

  # Airplanes.live — direct to COP
  python adapters/adsb_adapter.py --source airplaneslive --cop_url http://localhost:8100

  # File replay
  python adapters/adsb_adapter.py --source file --file data/aircraft.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

from shared.utils import utc_now_iso


def make_track_event(
    track_id: str,
    lat: float,
    lon: float,
    altitude_m: float,
    speed_mps: float,
    heading_deg: float,
    callsign: str,
    squawk: Optional[str],
    vertical_rate_mps: float,
    source_label: str,
) -> Dict[str, Any]:
    """Emit a track.update event with direct lat/lon (cop_publisher accepts this)."""
    intent = _classify_intent(speed_mps, vertical_rate_mps, altitude_m)
    threat_level = "LOW"  # ADS-B tracks are cooperative (identified) by definition

    return {
        "schema_version": "1.1",
        "event_id": str(uuid.uuid4()),
        "event_type": "track.update",
        "timestamp": utc_now_iso(),
        "source": {
            "agent_id": "adsb-adapter",
            "instance_id": source_label,
            "host": "local",
        },
        "correlation_id": track_id,
        "payload": {
            "global_track_id": f"ADSB-{track_id.upper()}",
            "id": f"ADSB-{track_id.upper()}",
            # Direct lat/lon — cop_publisher will use these instead of polar conversion
            "lat": round(lat, 7),
            "lon": round(lon, 7),
            "status": "CONFIRMED",
            "classification": {
                "label": "aircraft",
                "confidence": 0.99,
                "callsign": callsign.strip() if callsign else "",
                "squawk": squawk or "",
            },
            "supporting_sensors": ["adsb"],
            "kinematics": {
                "speed_mps": round(speed_mps, 2),
                "heading_deg": round(heading_deg, 1),
                "altitude_m": round(altitude_m, 1),
                "vertical_rate_mps": round(vertical_rate_mps, 2),
            },
            "intent": intent,
            "intent_conf": 0.6,
            "threat_level": threat_level,
            "threat_score": 0.1,
            "history": [],
        },
    }


def _classify_intent(speed_mps: float, vr_mps: float, alt_m: float) -> str:
    """Simple heuristic — real aircraft are cooperative so almost always 'unknown'."""
    if alt_m < 300 and speed_mps < 30:
        return "loitering"
    if vr_mps < -5 and alt_m < 1000:
        return "attack"      # rapidly descending low-level — not likely but flag it
    return "unknown"


def _knots_to_mps(knots: float) -> float:
    return knots * 0.514444


def _ft_to_m(ft: float) -> float:
    return ft * 0.3048


def _fpm_to_mps(fpm: float) -> float:
    return fpm * 0.00508


# ---------------------------------------------------------------------------
# dump1090 source
# ---------------------------------------------------------------------------

def fetch_dump1090(url: str) -> List[Dict[str, Any]]:
    """
    Poll dump1090 / readsb JSON API.
    Returns list of raw aircraft dicts.
    """
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        return data.get("aircraft", [])
    except Exception as e:
        print(f"[adsb] dump1090 fetch error: {e}", file=sys.stderr)
        return []


def parse_dump1090_aircraft(ac: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert dump1090 aircraft dict to normalised track fields."""
    lat = ac.get("lat")
    lon = ac.get("lon")
    if lat is None or lon is None:
        return None  # no position yet

    # altitude: prefer geometric, fall back to barometric
    alt_ft = ac.get("alt_geom") or ac.get("altitude") or 0
    speed_kt = ac.get("gs") or ac.get("speed") or 0        # ground speed
    heading = ac.get("track") or ac.get("mag_heading") or 0
    vr_fpm = ac.get("baro_rate") or ac.get("geom_rate") or 0
    callsign = ac.get("flight") or ac.get("r") or ""
    squawk = ac.get("squawk")
    icao = ac.get("hex", str(uuid.uuid4().hex[:6]))

    return {
        "track_id": icao,
        "lat": float(lat),
        "lon": float(lon),
        "altitude_m": _ft_to_m(float(alt_ft)),
        "speed_mps": _knots_to_mps(float(speed_kt)),
        "heading_deg": float(heading),
        "vertical_rate_mps": _fpm_to_mps(float(vr_fpm)),
        "callsign": callsign,
        "squawk": squawk,
    }


# ---------------------------------------------------------------------------
# OpenSky Network source
# ---------------------------------------------------------------------------

# OpenSky state vector column indices
_OS_ICAO24 = 0
_OS_CALLSIGN = 1
_OS_LAT = 6
_OS_LON = 5
_OS_BARO_ALT = 7
_OS_ON_GROUND = 8
_OS_VELOCITY = 9
_OS_HEADING = 10
_OS_VERT_RATE = 11
_OS_GEO_ALT = 13
_OS_SQUAWK = 14


def fetch_opensky(lat: float, lon: float, radius_km: float) -> List[Dict[str, Any]]:
    """
    Query OpenSky Network /states/all with a bounding box.
    No auth required; rate-limited to ~100 req/day unauthenticated.
    """
    deg = radius_km / 111.0
    lamin = lat - deg
    lamax = lat + deg
    lomin = lon - deg
    lomax = lon + deg

    url = (
        f"https://opensky-network.org/api/states/all"
        f"?lamin={lamin:.4f}&lamax={lamax:.4f}"
        f"&lomin={lomin:.4f}&lomax={lomax:.4f}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "NIZAM-adapter/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return data.get("states") or []
    except Exception as e:
        print(f"[adsb] OpenSky fetch error: {e}", file=sys.stderr)
        return []


def parse_opensky_state(state: list) -> Optional[Dict[str, Any]]:
    """Convert OpenSky state vector to normalised track fields."""
    if len(state) < 15:
        return None
    lat = state[_OS_LAT]
    lon = state[_OS_LON]
    if lat is None or lon is None:
        return None
    on_ground = state[_OS_ON_GROUND]
    if on_ground:
        return None  # skip ground traffic

    alt_m = state[_OS_GEO_ALT] or state[_OS_BARO_ALT] or 0
    speed_mps = state[_OS_VELOCITY] or 0
    heading = state[_OS_HEADING] or 0
    vr_mps = state[_OS_VERT_RATE] or 0
    callsign = (state[_OS_CALLSIGN] or "").strip()
    squawk = state[_OS_SQUAWK]
    icao = state[_OS_ICAO24] or uuid.uuid4().hex[:6]

    return {
        "track_id": icao,
        "lat": float(lat),
        "lon": float(lon),
        "altitude_m": float(alt_m),
        "speed_mps": float(speed_mps),
        "heading_deg": float(heading),
        "vertical_rate_mps": float(vr_mps),
        "callsign": callsign,
        "squawk": squawk,
    }


# ---------------------------------------------------------------------------
# ADSB.fi source  (free, no auth, generous limits)
# ---------------------------------------------------------------------------

def fetch_adsbfi(lat: float, lon: float, radius_km: float) -> List[Dict[str, Any]]:
    """
    Query api.adsb.fi/v1/aircraft with a lat/lon/radius bounding query.
    Returns dump1090-compatible aircraft dicts.
    """
    radius_nm = radius_km / 1.852
    url = (
        f"https://api.adsb.fi/v1/aircraft"
        f"?lat={lat:.4f}&lon={lon:.4f}&radius={radius_nm:.1f}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "NIZAM-adapter/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return data.get("aircraft", [])
    except Exception as e:
        print(f"[adsb] ADSB.fi fetch error: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Airplanes.live source  (free, no auth)
# ---------------------------------------------------------------------------

def fetch_airplaneslive(lat: float, lon: float, radius_km: float) -> List[Dict[str, Any]]:
    """
    Query api.airplanes.live/v2/point/{lat}/{lon}/{radius_nm}.
    Returns dump1090-compatible aircraft dicts (root key: "ac").
    """
    radius_nm = int(round(radius_km / 1.852))
    url = f"https://api.airplanes.live/v2/point/{lat:.4f}/{lon:.4f}/{radius_nm}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "NIZAM-adapter/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return data.get("ac", [])
    except Exception as e:
        print(f"[adsb] Airplanes.live fetch error: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# File source (dump1090 JSON snapshot)
# ---------------------------------------------------------------------------

def fetch_file(path: str) -> List[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return data.get("aircraft", [])
    except Exception as e:
        print(f"[adsb] file read error: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _emit(ev: Dict[str, Any], cop_url: str, api_key: str) -> None:
    """Write event to stdout or POST directly to COP /api/ingest."""
    line = json.dumps(ev, ensure_ascii=False)
    if not cop_url:
        print(line, flush=True)
        return
    data = line.encode()
    req = urllib.request.Request(
        f"{cop_url.rstrip('/')}/api/ingest",
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "NIZAM-adsb-adapter/1.0",
            **({"X-API-Key": api_key} if api_key else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception as exc:
        print(f"[adsb] POST error: {exc}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description="ADS-B → NIZAM track.update adapter")
    ap.add_argument("--source",
                    choices=["dump1090", "opensky", "adsbfi", "airplaneslive", "file"],
                    default="dump1090")
    ap.add_argument("--dump1090_url", default="http://localhost:8080/aircraft.json",
                    help="dump1090/readsb JSON endpoint")
    ap.add_argument("--lat", type=float, default=41.015,
                    help="Centre latitude for bounding-box sources")
    ap.add_argument("--lon", type=float, default=28.979,
                    help="Centre longitude for bounding-box sources")
    ap.add_argument("--radius_km", type=float, default=200.0,
                    help="Bounding-box radius in km (OpenSky / ADSB.fi / Airplanes.live)")
    ap.add_argument("--file", default="aircraft.json",
                    help="Path to dump1090 JSON snapshot (--source file)")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="Poll interval in seconds (ignored for --source file)")
    ap.add_argument("--min_alt_m", type=float, default=0.0,
                    help="Filter out tracks below this altitude (m)")
    ap.add_argument("--max_tracks", type=int, default=200,
                    help="Cap on number of tracks emitted per poll cycle")
    ap.add_argument("--cop_url", default="",
                    help="POST directly to COP (e.g. http://localhost:8100). "
                         "If omitted, output is JSONL on stdout.")
    ap.add_argument("--api_key", default="",
                    help="X-API-Key header value for COP ingest (when --cop_url is set)")
    args = ap.parse_args()

    cop_url = args.cop_url or ""
    print(f"[adsb] source={args.source} interval={args.interval}s "
          f"output={'COP HTTP' if cop_url else 'stdout'}", file=sys.stderr)

    one_shot = args.source == "file"

    while True:
        if args.source == "dump1090":
            raw_list = fetch_dump1090(args.dump1090_url)
            parse_fn = parse_dump1090_aircraft
        elif args.source == "opensky":
            raw_list = fetch_opensky(args.lat, args.lon, args.radius_km)
            parse_fn = parse_opensky_state
        elif args.source == "adsbfi":
            raw_list = fetch_adsbfi(args.lat, args.lon, args.radius_km)
            parse_fn = parse_dump1090_aircraft
        elif args.source == "airplaneslive":
            raw_list = fetch_airplaneslive(args.lat, args.lon, args.radius_km)
            parse_fn = parse_dump1090_aircraft
        else:  # file
            raw_list = fetch_file(args.file)
            parse_fn = parse_dump1090_aircraft

        count = 0
        for raw in raw_list:
            if count >= args.max_tracks:
                break
            track = parse_fn(raw)
            if track is None:
                continue
            if track["altitude_m"] < args.min_alt_m:
                continue

            ev = make_track_event(
                track_id=track["track_id"],
                lat=track["lat"],
                lon=track["lon"],
                altitude_m=track["altitude_m"],
                speed_mps=track["speed_mps"],
                heading_deg=track["heading_deg"],
                callsign=track["callsign"],
                squawk=track.get("squawk"),
                vertical_rate_mps=track["vertical_rate_mps"],
                source_label=args.source,
            )
            _emit(ev, cop_url, args.api_key)
            count += 1

        print(f"[adsb] emitted {count} tracks", file=sys.stderr)

        if one_shot:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
