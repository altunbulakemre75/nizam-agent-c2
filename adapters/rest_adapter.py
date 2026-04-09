"""
rest_adapter.py  —  Generic REST/HTTP polling adapter for NIZAM

Polls any HTTP endpoint that returns JSON track data and maps fields
to NIZAM's track.update format via a simple field-mapping config.

Supports:
  - Single-object responses: {"lat": ..., "lon": ..., "id": ...}
  - Array responses: [{"lat":...}, ...]
  - Nested responses via dot-path field mapping: "position.latitude"

Config file format (JSON):
  {
    "url": "http://sensor.local/api/tracks",
    "interval_s": 1.0,
    "response_key": "tracks",        // optional: key to unwrap array from response
    "id_field":       "id",
    "lat_field":      "latitude",
    "lon_field":      "longitude",
    "speed_field":    "speed_knots", // optional
    "speed_scale":    0.514444,      // multiply speed_field by this → m/s (1.0 = already m/s)
    "heading_field":  "course",      // optional
    "altitude_field": "altitude_m",  // optional
    "label_field":    "type",        // optional: classification label
    "headers": {                     // optional: HTTP headers (auth tokens, etc.)
      "Authorization": "Bearer TOKEN"
    }
  }

Usage:
  python adapters/rest_adapter.py --config adapters/configs/my_sensor.json \
    | python agents/cop_publisher.py

  # Quick one-liner without config file:
  python adapters/rest_adapter.py \
    --url http://localhost:9000/api/objects \
    --id_field id --lat_field lat --lon_field lon \
    | python agents/cop_publisher.py
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


def dot_get(obj: Dict, path: str) -> Any:
    """Retrieve a nested value using dot notation: 'position.lat'"""
    for key in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def make_track_event(
    track_id: str,
    lat: float,
    lon: float,
    speed_mps: float,
    heading_deg: float,
    altitude_m: float,
    label: str,
    raw: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "schema_version": "1.1",
        "event_id": str(uuid.uuid4()),
        "event_type": "track.update",
        "timestamp": utc_now_iso(),
        "source": {
            "agent_id": "rest-adapter",
            "instance_id": "rest-01",
            "host": "local",
        },
        "correlation_id": track_id,
        "payload": {
            "global_track_id": f"REST-{track_id}",
            "id": f"REST-{track_id}",
            "lat": round(lat, 7),
            "lon": round(lon, 7),
            "status": "TENTATIVE",
            "classification": {
                "label": label or "unknown",
                "confidence": 0.7,
            },
            "supporting_sensors": ["rest"],
            "kinematics": {
                "speed_mps": round(speed_mps, 2),
                "heading_deg": round(heading_deg, 1),
                "altitude_m": round(altitude_m, 1),
            },
            "intent": "unknown",
            "intent_conf": 0.3,
            "threat_level": "LOW",
            "threat_score": 0.1,
            "history": [],
        },
    }


# ---------------------------------------------------------------------------
# Field extractor
# ---------------------------------------------------------------------------

class FieldMapper:
    def __init__(self, cfg: Dict[str, Any]):
        self.url          = cfg["url"]
        self.interval_s   = float(cfg.get("interval_s", 2.0))
        self.response_key = cfg.get("response_key")           # unwrap array from dict
        self.id_field     = cfg.get("id_field", "id")
        self.lat_field    = cfg.get("lat_field", "lat")
        self.lon_field    = cfg.get("lon_field", "lon")
        self.speed_field  = cfg.get("speed_field")
        self.speed_scale  = float(cfg.get("speed_scale", 1.0))
        self.heading_field = cfg.get("heading_field")
        self.alt_field    = cfg.get("altitude_field")
        self.label_field  = cfg.get("label_field")
        self.headers      = cfg.get("headers", {})

    def extract(self, obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        lat = dot_get(obj, self.lat_field)
        lon = dot_get(obj, self.lon_field)
        if lat is None or lon is None:
            return None
        try:
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            return None

        track_id = str(dot_get(obj, self.id_field) or uuid.uuid4().hex[:8])
        speed = float(dot_get(obj, self.speed_field) or 0) * self.speed_scale if self.speed_field else 0.0
        heading = float(dot_get(obj, self.heading_field) or 0) if self.heading_field else 0.0
        altitude = float(dot_get(obj, self.alt_field) or 0) if self.alt_field else 0.0
        label = str(dot_get(obj, self.label_field) or "") if self.label_field else ""

        return {
            "track_id": track_id,
            "lat": lat,
            "lon": lon,
            "speed_mps": speed,
            "heading_deg": heading,
            "altitude_m": altitude,
            "label": label,
        }


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------

def fetch(url: str, headers: Dict[str, str]) -> Optional[Any]:
    req = urllib.request.Request(url, headers={
        "User-Agent": "NIZAM-rest-adapter/1.0",
        **headers,
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"[rest] HTTP {e.code}: {url}", file=sys.stderr)
    except Exception as e:
        print(f"[rest] fetch error: {e}", file=sys.stderr)
    return None


def unwrap_response(data: Any, response_key: Optional[str]) -> List[Dict]:
    """Return a list of objects from the response."""
    if response_key and isinstance(data, dict):
        data = data.get(response_key, [])
    if isinstance(data, dict):
        return [data]   # single-object endpoint
    if isinstance(data, list):
        return data
    return []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_config_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "url": args.url,
        "interval_s": args.interval,
        "id_field": args.id_field,
        "lat_field": args.lat_field,
        "lon_field": args.lon_field,
    }
    if args.speed_field:
        cfg["speed_field"] = args.speed_field
        cfg["speed_scale"] = args.speed_scale
    if args.heading_field:
        cfg["heading_field"] = args.heading_field
    if args.response_key:
        cfg["response_key"] = args.response_key
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser(description="Generic REST → NIZAM track.update adapter")
    ap.add_argument("--config", help="Path to JSON config file")
    ap.add_argument("--url", default="", help="Sensor REST endpoint URL")
    ap.add_argument("--interval", type=float, default=2.0, help="Poll interval (s)")
    ap.add_argument("--id_field", default="id")
    ap.add_argument("--lat_field", default="lat")
    ap.add_argument("--lon_field", default="lon")
    ap.add_argument("--speed_field", default="")
    ap.add_argument("--speed_scale", type=float, default=1.0)
    ap.add_argument("--heading_field", default="")
    ap.add_argument("--response_key", default="",
                    help="Key to unwrap list from response object")
    ap.add_argument("--once", action="store_true", help="Single poll then exit")
    args = ap.parse_args()

    if args.config:
        with open(args.config, encoding="utf-8") as f:
            cfg = json.load(f)
    elif args.url:
        cfg = build_config_from_args(args)
    else:
        ap.error("Provide --config or --url")

    mapper = FieldMapper(cfg)
    print(f"[rest] polling {mapper.url} every {mapper.interval_s}s", file=sys.stderr)

    while True:
        data = fetch(mapper.url, mapper.headers)
        if data is not None:
            objects = unwrap_response(data, mapper.response_key)
            count = 0
            for obj in objects:
                if not isinstance(obj, dict):
                    continue
                track = mapper.extract(obj)
                if track is None:
                    continue
                ev = make_track_event(**track, raw=obj)
                print(json.dumps(ev, ensure_ascii=False), flush=True)
                count += 1
            print(f"[rest] emitted {count} tracks", file=sys.stderr)

        if args.once:
            break
        time.sleep(mapper.interval_s)


if __name__ == "__main__":
    main()
