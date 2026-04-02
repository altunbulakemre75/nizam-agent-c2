"""
cop_publisher.py  —  NIZAM pipeline → COP bridge

Reads JSONL events from stdin, translates them into COP ingest payloads,
and POSTs them to the COP server's /ingest endpoint.

Supported input event types:
  track.update      → cop.track   (with range/az → lat/lon conversion)
  threat.assessment → cop.threat

Usage (pipeline tail):
  python agents/cop_publisher.py [--cop_url URL] [--origin_lat LAT] [--origin_lon LON]

Example full pipeline:
  python agents/world/world_agent.py \
    | python agents/radar_sim/radar_sim_agent.py \
    | tee >(python agents/rf_sim/rf_sim_agent.py) \
    | python agents/fuser/fuser_agent.py \
    | python agents/cop_publisher.py
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------

def polar_to_latlon(
    range_m: float,
    az_deg: float,
    origin_lat: float,
    origin_lon: float,
) -> tuple[float, float]:
    """
    Convert sensor-relative polar coords to WGS-84 lat/lon.

    Convention: az_deg is compass bearing (clockwise from North).
    Positive range = distance from sensor origin.
    """
    az_rad = math.radians(az_deg)
    # Displacement in meters
    dx = range_m * math.sin(az_rad)   # East
    dy = range_m * math.cos(az_rad)   # North

    # Metres per degree at origin latitude
    lat_deg_per_m = 1.0 / 111_320.0
    lon_deg_per_m = 1.0 / (111_320.0 * math.cos(math.radians(origin_lat)))

    lat = origin_lat + dy * lat_deg_per_m
    lon = origin_lon + dx * lon_deg_per_m
    return round(lat, 7), round(lon, 7)


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def post_json(url: str, body: Dict[str, Any], timeout: float = 3.0) -> bool:
    """POST a JSON body; returns True on 2xx, False otherwise."""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        print(f"[cop_publisher] HTTP {e.code} → {url}: {e.read()[:200]}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[cop_publisher] POST failed → {url}: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Translators
# ---------------------------------------------------------------------------

def translate_track_update(payload: Dict[str, Any], origin_lat: float, origin_lon: float) -> Dict[str, Any]:
    """
    track.update payload → cop.track payload.

    Adds lat/lon converted from kinematics.range_m + kinematics.az_deg.
    Falls back to origin coords if kinematics are missing.
    """
    gid = (
        payload.get("global_track_id")
        or payload.get("track_id")
        or payload.get("id")
        or "UNKNOWN"
    )

    kin = payload.get("kinematics") or {}
    range_m: Optional[float] = kin.get("range_m")
    az_deg: Optional[float] = kin.get("az_deg")

    if range_m is not None and az_deg is not None:
        lat, lon = polar_to_latlon(float(range_m), float(az_deg), origin_lat, origin_lon)
    else:
        lat, lon = origin_lat, origin_lon

    return {
        # Keys for COP server state store
        "global_track_id": gid,
        "id": gid,
        # Keys for Leaflet map rendering
        "lat": lat,
        "lon": lon,
        # Passthrough fields
        "status": payload.get("status", "TENTATIVE"),
        "classification": payload.get("classification", {}),
        "supporting_sensors": payload.get("supporting_sensors", []),
        "kinematics": kin,
        "server_time": payload.get("server_time"),
    }


def translate_threat_assessment(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    threat.assessment payload → cop.threat payload.
    """
    gid = (
        payload.get("global_track_id")
        or payload.get("track_id")
        or payload.get("id")
        or "UNKNOWN"
    )

    return {
        "global_track_id": gid,
        "id": gid,
        "threat_level": payload.get("threat_level", "LOW"),
        "score": payload.get("score", 0),
        "tti_s": payload.get("tti_s"),
        "recommended_action": payload.get("recommended_action", "OBSERVE"),
        "reasons": payload.get("reasons", []),
        "rules_fired": payload.get("rules_fired", []),
        "server_time": payload.get("server_time"),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="NIZAM pipeline → COP bridge: reads track/threat events from stdin, POSTs to COP /ingest"
    )
    ap.add_argument("--cop_url", default="http://127.0.0.1:8100", help="COP server base URL")
    ap.add_argument("--origin_lat", type=float, default=41.015, help="Sensor origin latitude (WGS-84)")
    ap.add_argument("--origin_lon", type=float, default=28.979, help="Sensor origin longitude (WGS-84)")
    ap.add_argument("--retry_delay", type=float, default=0.5, help="Seconds to wait before retrying a failed POST")
    ap.add_argument("--passthrough", action="store_true", help="Echo each input line to stdout (for chaining)")
    args = ap.parse_args()

    ingest_url = args.cop_url.rstrip("/") + "/ingest"
    print(f"[cop_publisher] Starting. Ingesting to: {ingest_url}", file=sys.stderr)
    print(f"[cop_publisher] Origin: lat={args.origin_lat}, lon={args.origin_lon}", file=sys.stderr)

    sent = 0
    skipped = 0

    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue

        if args.passthrough:
            print(raw_line, flush=True)

        try:
            ev = json.loads(raw_line)
        except json.JSONDecodeError as e:
            print(f"[cop_publisher] JSON parse error: {e} | line: {raw_line[:80]}", file=sys.stderr)
            skipped += 1
            continue

        event_type: str = ev.get("event_type", "")
        payload: Dict[str, Any] = ev.get("payload") or {}

        if event_type == "track.update":
            cop_payload = translate_track_update(payload, args.origin_lat, args.origin_lon)
            body = {"event_type": "cop.track", "payload": cop_payload}

        elif event_type == "threat.assessment":
            cop_payload = translate_threat_assessment(payload)
            body = {"event_type": "cop.threat", "payload": cop_payload}

        else:
            # Silently ignore unrecognised event types (world.state, sensor.detection.*, etc.)
            continue

        ok = post_json(ingest_url, body, timeout=3.0)
        if ok:
            sent += 1
            if sent % 50 == 0:
                print(f"[cop_publisher] {sent} events sent.", file=sys.stderr)
        else:
            # Brief pause so we don't spam a dead server
            time.sleep(args.retry_delay)


if __name__ == "__main__":
    main()
