"""
ais_adapter.py  —  AIS maritime sensor adapter for NIZAM

Reads AIS (Automatic Identification System) vessel positions from:
  --source tcp    : NMEA-0183 TCP stream (AISHub, Kystverket, local VHF receiver)
  --source serial : COM port / /dev/ttyUSB0 (VHF radio + AIS decoder)
  --source file   : NMEA sentence file (for testing, one sentence per line)

Decodes AIS message types 1, 2, 3 (Class A position reports)
and type 18 (Class B position reports) without external libraries.

Outputs track.update JSONL to stdout → pipe into cop_publisher.py

Usage:
  # TCP stream (e.g. AISHub relay or local SignalK/OpenCPN)
  python adapters/ais_adapter.py --source tcp --host 127.0.0.1 --port 10110

  # Serial port (Windows: COM3, Linux: /dev/ttyUSB0)
  python adapters/ais_adapter.py --source serial --port COM3 --baud 38400

  # File replay
  python adapters/ais_adapter.py --source file --file data/ais_sample.nmea
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# AIS bit-twiddling helpers (no external deps)
# ---------------------------------------------------------------------------

_AIS_CHARSET = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"


def _ascii6_decode(raw: str) -> str:
    """Convert NMEA armoured payload characters to 6-bit binary string."""
    bits = ""
    for ch in raw:
        val = ord(ch) - 48
        if val > 40:
            val -= 8
        bits += format(val, "06b")
    return bits


def _bits_uint(bits: str, start: int, length: int) -> int:
    return int(bits[start:start + length], 2)


def _bits_int(bits: str, start: int, length: int) -> int:
    """Two's-complement signed integer."""
    val = _bits_uint(bits, start, length)
    if bits[start] == "1":
        val -= (1 << length)
    return val


def _bits_str(bits: str, start: int, length: int) -> str:
    """6-bit ASCII string (AIS text encoding)."""
    result = ""
    for i in range(0, length, 6):
        idx = _bits_uint(bits, start + i, 6)
        result += _AIS_CHARSET[idx] if idx < len(_AIS_CHARSET) else "@"
    return result.rstrip("@").strip()


def _decode_mmsi(bits: str) -> str:
    return str(_bits_uint(bits, 8, 30))


# ---------------------------------------------------------------------------
# AIS message parsers
# ---------------------------------------------------------------------------

def parse_type_1_2_3(bits: str) -> Optional[Dict[str, Any]]:
    """Class A position report (message types 1, 2, 3)."""
    if len(bits) < 137:   # hdg field ends at bit 136; 168 is nominal but short payloads are common
        return None
    mmsi = _decode_mmsi(bits)
    status = _bits_uint(bits, 38, 4)
    sog = _bits_uint(bits, 50, 10) / 10.0         # knots
    lon = _bits_int(bits, 61, 28) / 600000.0       # degrees
    lat = _bits_int(bits, 89, 27) / 600000.0       # degrees
    cog = _bits_uint(bits, 116, 12) / 10.0         # degrees
    hdg = _bits_uint(bits, 128, 9)                  # degrees (511 = not available)

    if lat == 0.0 and lon == 0.0:
        return None
    if abs(lat) > 90 or abs(lon) > 180:
        return None

    heading_deg = float(hdg) if hdg < 360 else cog

    return {
        "mmsi": mmsi,
        "lat": round(lat, 7),
        "lon": round(lon, 7),
        "speed_mps": sog * 0.514444,
        "heading_deg": round(heading_deg, 1),
        "nav_status": status,
        "vessel_class": "A",
    }


def parse_type_18(bits: str) -> Optional[Dict[str, Any]]:
    """Class B position report (message type 18)."""
    if len(bits) < 168:
        return None
    mmsi = _decode_mmsi(bits)
    sog = _bits_uint(bits, 46, 10) / 10.0
    lon = _bits_int(bits, 57, 28) / 600000.0
    lat = _bits_int(bits, 85, 27) / 600000.0
    cog = _bits_uint(bits, 112, 12) / 10.0
    hdg = _bits_uint(bits, 124, 9)

    if lat == 0.0 and lon == 0.0:
        return None
    if abs(lat) > 90 or abs(lon) > 180:
        return None

    heading_deg = float(hdg) if hdg < 360 else cog

    return {
        "mmsi": mmsi,
        "lat": round(lat, 7),
        "lon": round(lon, 7),
        "speed_mps": sog * 0.514444,
        "heading_deg": round(heading_deg, 1),
        "nav_status": 15,  # undefined for Class B
        "vessel_class": "B",
    }


# Nav status codes (AIS spec)
_NAV_STATUS = {
    0: "under_way_engine",
    1: "at_anchor",
    2: "not_under_command",
    3: "restricted_maneuverability",
    5: "moored",
    8: "under_way_sailing",
    15: "undefined",
}


def _nav_to_intent(nav_status: int, speed_mps: float) -> str:
    if nav_status in (1, 5):
        return "loitering"
    if speed_mps > 10:
        return "unknown"
    return "unknown"


# ---------------------------------------------------------------------------
# NMEA sentence decoder
# ---------------------------------------------------------------------------

def decode_nmea_sentence(sentence: str) -> Optional[Dict[str, Any]]:
    """
    Parse a single !AIVDM / !AIVDO NMEA sentence.
    Returns normalised vessel dict or None if not a supported AIS sentence.
    Note: ignores multi-part messages (parts 2+ of a sequence) for simplicity.
    """
    sentence = sentence.strip()
    if not sentence.startswith(("!AIVDM", "!AIVDO")):
        return None

    # Validate checksum
    if "*" in sentence:
        body, chk = sentence.rsplit("*", 1)
        body = body[1:]  # strip leading !
        expected = 0
        for ch in body:
            expected ^= ord(ch)
        if f"{expected:02X}" != chk[:2].upper():
            return None  # checksum mismatch
        sentence = "!" + body

    parts = sentence.split(",")
    if len(parts) < 6:
        return None

    frag_count = int(parts[1]) if parts[1].isdigit() else 1
    frag_num   = int(parts[2]) if parts[2].isdigit() else 1

    # Only handle single-part or first fragment (good enough for position types)
    if frag_count > 1 and frag_num > 1:
        return None

    payload = parts[5]
    if not payload:
        return None

    try:
        bits = _ascii6_decode(payload)
    except Exception:
        return None

    if len(bits) < 6:
        return None

    msg_type = _bits_uint(bits, 0, 6)

    if msg_type in (1, 2, 3):
        return parse_type_1_2_3(bits)
    if msg_type == 18:
        return parse_type_18(bits)

    return None  # other message types (5=static data, 21=aid-to-nav, etc.)


# ---------------------------------------------------------------------------
# Event builder
# ---------------------------------------------------------------------------

def make_track_event(vessel: Dict[str, Any]) -> Dict[str, Any]:
    intent = _nav_to_intent(vessel["nav_status"], vessel["speed_mps"])
    nav_label = _NAV_STATUS.get(vessel["nav_status"], "unknown")

    return {
        "schema_version": "1.1",
        "event_id": str(uuid.uuid4()),
        "event_type": "track.update",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": {
            "agent_id": "ais-adapter",
            "instance_id": "ais-01",
            "host": "local",
        },
        "correlation_id": vessel["mmsi"],
        "payload": {
            "global_track_id": f"AIS-{vessel['mmsi']}",
            "id": f"AIS-{vessel['mmsi']}",
            "lat": vessel["lat"],
            "lon": vessel["lon"],
            "status": "CONFIRMED",
            "classification": {
                "label": "vessel",
                "confidence": 0.99,
                "vessel_class": vessel["vessel_class"],
                "nav_status": nav_label,
                "mmsi": vessel["mmsi"],
            },
            "supporting_sensors": ["ais"],
            "kinematics": {
                "speed_mps": round(vessel["speed_mps"], 2),
                "heading_deg": vessel["heading_deg"],
            },
            "intent": intent,
            "intent_conf": 0.5,
            "threat_level": "LOW",
            "threat_score": 0.05,
            "history": [],
        },
    }


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def read_tcp(host: str, port: int):
    """Yield NMEA sentences from a TCP stream (reconnects on error)."""
    while True:
        try:
            print(f"[ais] connecting to {host}:{port}", file=sys.stderr)
            with socket.create_connection((host, port), timeout=30) as sock:
                buf = b""
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        yield line.decode("ascii", errors="ignore")
        except Exception as e:
            print(f"[ais] TCP error: {e} — reconnecting in 5s", file=sys.stderr)
            time.sleep(5)


def read_serial(port: str, baud: int):
    """Yield NMEA sentences from a serial port."""
    try:
        import serial  # pyserial
    except ImportError:
        print("[ais] ERROR: pyserial not installed. Run: pip install pyserial", file=sys.stderr)
        sys.exit(1)

    while True:
        try:
            print(f"[ais] opening serial {port} @ {baud}", file=sys.stderr)
            with serial.Serial(port, baud, timeout=1) as ser:
                while True:
                    line = ser.readline().decode("ascii", errors="ignore")
                    if line:
                        yield line
        except Exception as e:
            print(f"[ais] serial error: {e} — retrying in 5s", file=sys.stderr)
            time.sleep(5)


def read_file(path: str):
    """Yield NMEA sentences from a text file (one sentence per line)."""
    try:
        with open(path, encoding="ascii", errors="ignore") as f:
            for line in f:
                yield line
    except Exception as e:
        print(f"[ais] file error: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="AIS maritime → NIZAM track.update adapter")
    ap.add_argument("--source", choices=["tcp", "serial", "file"], default="tcp")
    ap.add_argument("--host", default="127.0.0.1", help="TCP host")
    ap.add_argument("--port", default=10110, type=int, help="TCP port")
    ap.add_argument("--serial_port", default="COM3", help="Serial port (--source serial)")
    ap.add_argument("--baud", default=38400, type=int)
    ap.add_argument("--file", default="data/ais_sample.nmea", help="NMEA file (--source file)")
    ap.add_argument("--lat_min", type=float, default=-90.0)
    ap.add_argument("--lat_max", type=float, default=90.0)
    ap.add_argument("--lon_min", type=float, default=-180.0)
    ap.add_argument("--lon_max", type=float, default=180.0)
    args = ap.parse_args()

    print(f"[ais] source={args.source}", file=sys.stderr)

    if args.source == "tcp":
        sentences = read_tcp(args.host, args.port)
    elif args.source == "serial":
        sentences = read_serial(args.serial_port, args.baud)
    else:
        sentences = read_file(args.file)

    count = 0
    for sentence in sentences:
        vessel = decode_nmea_sentence(sentence)
        if vessel is None:
            continue

        # Bounding box filter
        if not (args.lat_min <= vessel["lat"] <= args.lat_max):
            continue
        if not (args.lon_min <= vessel["lon"] <= args.lon_max):
            continue

        ev = make_track_event(vessel)
        print(json.dumps(ev, ensure_ascii=False), flush=True)
        count += 1
        if count % 50 == 0:
            print(f"[ais] {count} vessel tracks emitted", file=sys.stderr)


if __name__ == "__main__":
    main()
