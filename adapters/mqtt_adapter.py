"""
mqtt_adapter.py  —  MQTT sensor adapter for NIZAM

Subscribes to an MQTT broker topic, parses incoming JSON messages into
NIZAM's track.update format, and outputs JSONL to stdout → pipe into
cop_publisher.py  OR  posts directly to COP via HTTP (--cop_url).

Supports flexible field mapping (same dot-path syntax as rest_adapter),
optional TLS, per-track rate limiting, and pass-through for messages
that are already in track.update format.

Requirements:
  pip install paho-mqtt          (added to requirements.txt)

Usage:
  # Local Mosquitto broker, default topic "nizam/tracks"
  python adapters/mqtt_adapter.py --broker 127.0.0.1 --port 1883 \
    | python agents/cop_publisher.py

  # Direct HTTP POST to COP (no piping needed)
  python adapters/mqtt_adapter.py --broker 127.0.0.1 \
    --cop_url http://localhost:8100

  # TLS-enabled broker, multiple topics, rate-limited to 2 Hz per track
  python adapters/mqtt_adapter.py \
    --broker mqtt.example.com --port 8883 --tls \
    --username sensor1 --password secret \
    --topic sensors/radar/+ --topic sensors/iff/+ \
    --rate_limit 2.0 \
    | python agents/cop_publisher.py

  # Custom field mapping (sensor sends {"position":{"latitude":..., "longitude":...}})
  python adapters/mqtt_adapter.py \
    --broker 127.0.0.1 --topic sensors/# \
    --id_field id --lat_field position.latitude --lon_field position.longitude \
    | python agents/cop_publisher.py
"""

from __future__ import annotations

import argparse
import json
import queue
import ssl
import sys
import threading
import time
import urllib.request
import uuid
from typing import Any, Dict, Optional

from shared.utils import utc_now_iso


# ---------------------------------------------------------------------------
# Per-track rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Allow at most `rate_hz` events per second per track ID.  0 = unlimited."""

    def __init__(self, rate_hz: float):
        self._min_interval = (1.0 / rate_hz) if rate_hz > 0 else 0.0
        self._last: Dict[str, float] = {}

    def allow(self, track_id: str) -> bool:
        if self._min_interval == 0.0:
            return True
        now = time.monotonic()
        if now - self._last.get(track_id, 0.0) < self._min_interval:
            return False
        self._last[track_id] = now
        return True


# ---------------------------------------------------------------------------
# Output handler: stdout JSONL  OR  direct HTTP POST to COP /api/ingest
# ---------------------------------------------------------------------------

class OutputHandler:
    """Emit track events to stdout or directly POST to COP.

    When cop_url is given a background worker thread drains a queue and
    sends each event to ``{cop_url}/api/ingest`` with optional API key.
    """

    def __init__(self, cop_url: Optional[str], api_key: str):
        self._cop_url = cop_url.rstrip("/") if cop_url else None
        self._api_key = api_key
        self._queue: queue.Queue[Dict[str, Any]] = queue.Queue(maxsize=1000)
        self._dropped = 0
        if self._cop_url:
            threading.Thread(target=self._worker, daemon=True, name="mqtt-poster").start()
            print(f"[mqtt] direct-POST mode → {self._cop_url}/api/ingest", file=sys.stderr)

    def emit(self, event: Dict[str, Any]) -> None:
        if self._cop_url:
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                self._dropped += 1
                if self._dropped % 100 == 1:
                    print(f"[mqtt] output queue full — {self._dropped} events dropped",
                          file=sys.stderr)
        else:
            print(json.dumps(event, ensure_ascii=False), flush=True)

    def _worker(self) -> None:
        url = f"{self._cop_url}/api/ingest"
        while True:
            event = self._queue.get()
            try:
                data = json.dumps(event).encode()
                headers: Dict[str, str] = {"Content-Type": "application/json"}
                if self._api_key:
                    headers["X-API-Key"] = self._api_key
                req = urllib.request.Request(url, data=data, headers=headers, method="POST")
                urllib.request.urlopen(req, timeout=5)
            except Exception as exc:
                print(f"[mqtt] POST failed: {exc}", file=sys.stderr)
            finally:
                self._queue.task_done()


# ---------------------------------------------------------------------------
# Field extraction (dot-path, same as rest_adapter)
# ---------------------------------------------------------------------------

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
) -> Dict[str, Any]:
    return {
        "schema_version": "1.1",
        "event_id": str(uuid.uuid4()),
        "event_type": "track.update",
        "timestamp": utc_now_iso(),
        "source": {
            "agent_id": "mqtt-adapter",
            "instance_id": "mqtt-01",
            "host": "local",
        },
        "correlation_id": track_id,
        "payload": {
            "global_track_id": f"MQTT-{track_id}",
            "id": f"MQTT-{track_id}",
            "lat": round(lat, 7),
            "lon": round(lon, 7),
            "status": "TENTATIVE",
            "classification": {
                "label": label or "unknown",
                "confidence": 0.7,
            },
            "supporting_sensors": ["mqtt"],
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
# Message parser
# ---------------------------------------------------------------------------

class MessageParser:
    def __init__(self, args: argparse.Namespace):
        self.id_field = args.id_field
        self.lat_field = args.lat_field
        self.lon_field = args.lon_field
        self.speed_field = args.speed_field or None
        self.speed_scale = args.speed_scale
        self.heading_field = args.heading_field or None
        self.alt_field = args.altitude_field or None
        self.label_field = args.label_field or None

    def parse(self, payload_bytes: bytes) -> Optional[Dict[str, Any]]:
        try:
            obj = json.loads(payload_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError):
            print("[mqtt] malformed JSON payload ignored", file=sys.stderr)
            return None

        if not isinstance(obj, dict):
            return None

        # Pass-through: message is already a NIZAM track.update event
        if obj.get("event_type") == "track.update" and "payload" in obj:
            return obj

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

        return make_track_event(
            track_id=track_id,
            lat=lat,
            lon=lon,
            speed_mps=speed,
            heading_deg=heading,
            altitude_m=altitude,
            label=label,
        )


# ---------------------------------------------------------------------------
# MQTT client
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> None:
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("[mqtt] ERROR: paho-mqtt not installed. Run: pip install paho-mqtt",
              file=sys.stderr)
        sys.exit(1)

    msg_parser  = MessageParser(args)
    rate_limiter = RateLimiter(args.rate_limit)
    output      = OutputHandler(getattr(args, "cop_url", None), getattr(args, "api_key", ""))
    count       = 0
    skipped     = 0
    topics      = args.topic  # list (action="append")

    def on_connect(client, userdata, flags, rc, properties=None):
        if rc == 0:
            print(f"[mqtt] connected to {args.broker}:{args.port}", file=sys.stderr)
            for t in topics:
                client.subscribe(t, qos=args.qos)
                print(f"[mqtt] subscribed to '{t}' (QoS {args.qos})", file=sys.stderr)
        else:
            print(f"[mqtt] connection failed rc={rc}", file=sys.stderr)

    def on_message(client, userdata, msg):
        nonlocal count, skipped
        ev = msg_parser.parse(msg.payload)
        if ev is None:
            return

        track_id = ev.get("correlation_id") or ev.get("payload", {}).get("id", "?")
        if not rate_limiter.allow(str(track_id)):
            skipped += 1
            return

        output.emit(ev)
        count += 1
        if count % 100 == 0:
            print(f"[mqtt] emitted={count} rate-skipped={skipped}", file=sys.stderr)

    def on_disconnect(client, userdata, rc, properties=None):
        if rc != 0:
            print(f"[mqtt] unexpected disconnect rc={rc}, will auto-reconnect",
                  file=sys.stderr)

    client = mqtt.Client(
        client_id=f"nizam-mqtt-{uuid.uuid4().hex[:8]}",
        protocol=mqtt.MQTTv5,
    )

    if args.username:
        client.username_pw_set(args.username, args.password)

    if args.tls:
        ctx = ssl.create_default_context()
        if args.tls_insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        client.tls_set_context(ctx)

    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    print(f"[mqtt] connecting to {args.broker}:{args.port} topics={topics}",
          file=sys.stderr)
    client.connect(args.broker, args.port, keepalive=args.keepalive)
    client.loop_forever()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="MQTT → NIZAM track.update adapter")

    # Broker connection
    ap.add_argument("--broker", default="127.0.0.1", help="MQTT broker hostname")
    ap.add_argument("--port", type=int, default=1883, help="MQTT broker port")
    ap.add_argument("--topic", default=None, action="append",
                    help="MQTT topic to subscribe (repeatable; wildcards +/# supported)")
    ap.add_argument("--qos", type=int, default=1, choices=[0, 1, 2],
                    help="MQTT QoS level")
    ap.add_argument("--keepalive", type=int, default=60,
                    help="MQTT keepalive interval in seconds")
    ap.add_argument("--username", default="", help="MQTT username")
    ap.add_argument("--password", default="", help="MQTT password")
    ap.add_argument("--tls", action="store_true", help="Enable TLS")
    ap.add_argument("--tls_insecure", action="store_true",
                    help="Skip TLS certificate verification (testing only)")

    # Output
    ap.add_argument("--cop_url", default="",
                    help="POST directly to COP (e.g. http://localhost:8100). "
                         "If omitted, output is JSONL on stdout.")
    ap.add_argument("--api_key", default="",
                    help="X-API-Key header value for COP ingest (when --cop_url is set)")

    # Rate limiting
    ap.add_argument("--rate_limit", type=float, default=0.0,
                    help="Max track.update events per second per track ID (0 = unlimited)")

    # Field mapping
    ap.add_argument("--id_field", default="id", help="Dot-path to track ID field")
    ap.add_argument("--lat_field", default="lat", help="Dot-path to latitude field")
    ap.add_argument("--lon_field", default="lon", help="Dot-path to longitude field")
    ap.add_argument("--speed_field", default="", help="Dot-path to speed field")
    ap.add_argument("--speed_scale", type=float, default=1.0,
                    help="Multiply speed by this to get m/s")
    ap.add_argument("--heading_field", default="", help="Dot-path to heading field")
    ap.add_argument("--altitude_field", default="", help="Dot-path to altitude field")
    ap.add_argument("--label_field", default="", help="Dot-path to classification label")

    args = ap.parse_args()
    # Default topic when none specified
    if not args.topic:
        args.topic = ["nizam/tracks"]
    # Normalise cop_url
    args.cop_url = args.cop_url or None
    run(args)


if __name__ == "__main__":
    main()
