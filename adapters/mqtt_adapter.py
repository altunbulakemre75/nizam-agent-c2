"""
mqtt_adapter.py  —  MQTT sensor adapter for NIZAM

Subscribes to an MQTT broker topic, parses incoming JSON messages into
NIZAM's track.update format, and outputs JSONL to stdout → pipe into
cop_publisher.py.

Supports flexible field mapping (same dot-path syntax as rest_adapter)
and optional TLS for secure brokers.

Requirements:
  pip install paho-mqtt          (added to requirements.txt)

Usage:
  # Local Mosquitto broker, default topic "nizam/tracks"
  python adapters/mqtt_adapter.py --broker 127.0.0.1 --port 1883 \
    | python agents/cop_publisher.py

  # TLS-enabled broker with auth
  python adapters/mqtt_adapter.py \
    --broker mqtt.example.com --port 8883 --tls \
    --username sensor1 --password secret \
    --topic sensors/radar/+ \
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
import ssl
import sys
import uuid
from typing import Any, Dict, Optional

from shared.utils import utc_now_iso


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
            return None

        if not isinstance(obj, dict):
            return None

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

    parser = MessageParser(args)
    count = 0

    def on_connect(client, userdata, flags, rc, properties=None):
        if rc == 0:
            print(f"[mqtt] connected to {args.broker}:{args.port}", file=sys.stderr)
            client.subscribe(args.topic, qos=args.qos)
            print(f"[mqtt] subscribed to '{args.topic}' (QoS {args.qos})", file=sys.stderr)
        else:
            print(f"[mqtt] connection failed rc={rc}", file=sys.stderr)

    def on_message(client, userdata, msg):
        nonlocal count
        ev = parser.parse(msg.payload)
        if ev is None:
            return
        print(json.dumps(ev, ensure_ascii=False), flush=True)
        count += 1
        if count % 50 == 0:
            print(f"[mqtt] {count} tracks emitted", file=sys.stderr)

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

    print(f"[mqtt] connecting to {args.broker}:{args.port} topic='{args.topic}'",
          file=sys.stderr)
    client.connect(args.broker, args.port, keepalive=60)
    client.loop_forever()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="MQTT → NIZAM track.update adapter")

    # Broker connection
    ap.add_argument("--broker", default="127.0.0.1", help="MQTT broker hostname")
    ap.add_argument("--port", type=int, default=1883, help="MQTT broker port")
    ap.add_argument("--topic", default="nizam/tracks",
                    help="MQTT topic to subscribe (wildcards +/# supported)")
    ap.add_argument("--qos", type=int, default=1, choices=[0, 1, 2],
                    help="MQTT QoS level")
    ap.add_argument("--username", default="", help="MQTT username")
    ap.add_argument("--password", default="", help="MQTT password")
    ap.add_argument("--tls", action="store_true", help="Enable TLS")
    ap.add_argument("--tls_insecure", action="store_true",
                    help="Skip TLS certificate verification (testing only)")

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
    run(args)


if __name__ == "__main__":
    main()
