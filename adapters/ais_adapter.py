"""
ais_adapter.py  —  AIS maritime sensor adapter for NIZAM

Reads AIS (Automatic Identification System) vessel positions from:
  --source tcp       : NMEA-0183 TCP stream (AISHub, SignalK, local VHF receiver)
  --source serial    : COM port / /dev/ttyUSB0 (VHF radio + AIS decoder)
  --source aisstream : aisstream.io WebSocket API (free, requires --ais_api_key)
  --source file      : NMEA sentence file (for testing, one sentence per line)

Decodes AIS message types 1, 2, 3 (Class A) and 18 (Class B) without external libs.
aisstream.io source uses a stdlib-only WebSocket client (no extra deps).

Output modes:
  stdout (default)    : JSONL → pipe into cop_publisher.py
  --cop_url http://.. : POST directly to COP /api/ingest (no pipe needed)

Usage:
  # TCP stream (e.g. AISHub relay or local SignalK/OpenCPN)
  python adapters/ais_adapter.py --source tcp --host 127.0.0.1 --port 10110

  # aisstream.io — Bosphorus bounding box, direct to COP
  python adapters/ais_adapter.py --source aisstream \\
    --ais_api_key YOUR_KEY --cop_url http://localhost:8100 \\
    --lat_min 40.5 --lat_max 41.5 --lon_min 28.0 --lon_max 30.0

  # Serial port (Windows: COM3, Linux: /dev/ttyUSB0)
  python adapters/ais_adapter.py --source serial --serial_port COM3 --baud 38400

  # File replay
  python adapters/ais_adapter.py --source file --file data/ais_sample.nmea
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import ssl
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Optional


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
                "conf": 0.99,
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
# HTTP POST output (--cop_url mode)
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
            "User-Agent": "NIZAM-ais-adapter/1.0",
            **({"X-API-Key": api_key} if api_key else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception as exc:
        print(f"[ais] POST error: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Stdlib-only WebSocket client (for aisstream.io — no extra deps)
# ---------------------------------------------------------------------------

def _ws_recv_n(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("WebSocket connection closed")
        data += chunk
    return data


def _ws_recv_frame(sock: socket.socket) -> Optional[str]:
    """Read one WebSocket frame. Returns text payload or None (non-text frame)."""
    header = _ws_recv_n(sock, 2)
    opcode     = header[0] & 0x0F
    payload_len = header[1] & 0x7F
    if payload_len == 126:
        payload_len = int.from_bytes(_ws_recv_n(sock, 2), "big")
    elif payload_len == 127:
        payload_len = int.from_bytes(_ws_recv_n(sock, 8), "big")
    payload = _ws_recv_n(sock, payload_len)
    if opcode == 8:   # Close
        raise ConnectionError("Server sent WebSocket close frame")
    if opcode == 9:   # Ping → send Pong
        sock.sendall(b"\x8a\x00")
        return None
    if opcode in (1, 0):  # Text or continuation
        return payload.decode("utf-8", errors="replace")
    return None


def _ws_send_text(sock: socket.socket, text: str) -> None:
    """Send a masked WebSocket text frame (client → server masking is required)."""
    data = text.encode("utf-8")
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    header = bytearray([0x81])
    n = len(data)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header += bytearray([0x80 | 126, n >> 8, n & 0xFF])
    else:
        header += bytearray([0x80 | 127]) + n.to_bytes(8, "big")
    sock.sendall(bytes(header) + mask + masked)


def _ws_connect(url: str) -> socket.socket:
    """Open a WebSocket connection (ws:// or wss://) using stdlib only."""
    use_ssl = url.startswith("wss://")
    host_path = url[6:] if use_ssl else url[5:]
    default_port = 443 if use_ssl else 80
    if "/" in host_path:
        host, path = host_path.split("/", 1)
        path = "/" + path
    else:
        host, path = host_path, "/"
    if ":" in host:
        host, port_s = host.rsplit(":", 1)
        port = int(port_s)
    else:
        port = default_port

    raw = socket.create_connection((host, port), timeout=30)
    if use_ssl:
        ctx = ssl.create_default_context()
        raw = ctx.wrap_socket(raw, server_hostname=host)  # type: ignore[assignment]

    key = base64.b64encode(hashlib.sha1(os.urandom(16)).digest()).decode()
    handshake = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    ).encode()
    raw.sendall(handshake)

    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = raw.recv(4096)
        if not chunk:
            raise ConnectionError("Server closed during WS handshake")
        resp += chunk
    if b"101" not in resp:
        raise ConnectionError(f"WS upgrade failed: {resp[:200]!r}")
    return raw  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# aisstream.io source
# ---------------------------------------------------------------------------

_AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"


def parse_aisstream_msg(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Parse an aisstream.io JSON message into normalised vessel fields."""
    msg_type = msg.get("MessageType", "")
    meta      = msg.get("MetaData", {})
    inner     = msg.get("Message", {}).get(msg_type, {})

    if msg_type not in ("PositionReport", "StandardClassBPositionReport",
                        "ExtendedClassBPositionReport"):
        return None

    mmsi = str(meta.get("MMSI") or inner.get("Mmsi", ""))
    if not mmsi:
        return None
    lat = meta.get("latitude") or inner.get("Latitude")
    lon = meta.get("longitude") or inner.get("Longitude")
    if lat is None or lon is None:
        return None

    sog = float(inner.get("Sog") or 0)
    cog = float(inner.get("Cog") or 0)
    hdg = inner.get("TrueHeading", 511)
    nav = inner.get("NavigationalStatus", 15)
    heading = float(hdg) if isinstance(hdg, (int, float)) and int(hdg) < 360 else cog
    vessel_class = "B" if "ClassB" in msg_type or "Extended" in msg_type else "A"

    return {
        "mmsi":        mmsi,
        "lat":         round(float(lat), 7),
        "lon":         round(float(lon), 7),
        "speed_mps":   sog * 0.514444,
        "heading_deg": round(heading, 1),
        "nav_status":  nav,
        "vessel_class": vessel_class,
    }


def read_aisstream(
    api_key: str,
    lat_min: float,
    lat_max: float,
    lon_min: float,
    lon_max: float,
) -> Iterator[Dict[str, Any]]:
    """Yield parsed vessel dicts from aisstream.io WebSocket (reconnects on error)."""
    sub = json.dumps({
        "APIKey": api_key,
        "BoundingBoxes": [[[lat_min, lon_min], [lat_max, lon_max]]],
    })
    while True:
        try:
            print(f"[ais] connecting to aisstream.io …", file=sys.stderr)
            sock = _ws_connect(_AISSTREAM_URL)
            _ws_send_text(sock, sub)
            print(f"[ais] aisstream.io connected, bbox=[{lat_min},{lon_min}→{lat_max},{lon_max}]",
                  file=sys.stderr)
            while True:
                text = _ws_recv_frame(sock)
                if text is None:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    continue
                vessel = parse_aisstream_msg(msg)
                if vessel:
                    yield vessel
        except Exception as exc:
            print(f"[ais] aisstream.io error: {exc} — reconnecting in 5s", file=sys.stderr)
            time.sleep(5)


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
    ap.add_argument("--source", choices=["tcp", "serial", "aisstream", "file"],
                    default="tcp")
    ap.add_argument("--host",        default="127.0.0.1", help="TCP host")
    ap.add_argument("--port",        default=10110, type=int, help="TCP port")
    ap.add_argument("--serial_port", default="COM3",  help="Serial port (--source serial)")
    ap.add_argument("--baud",        default=38400,   type=int)
    ap.add_argument("--file",        default="data/ais_sample.nmea",
                    help="NMEA file (--source file)")
    ap.add_argument("--ais_api_key", default="",
                    help="aisstream.io API key (--source aisstream)")
    ap.add_argument("--lat_min",     type=float, default=36.0,
                    help="Bounding box min latitude  (default: Turkey south)")
    ap.add_argument("--lat_max",     type=float, default=42.5,
                    help="Bounding box max latitude  (default: Turkey north)")
    ap.add_argument("--lon_min",     type=float, default=26.0,
                    help="Bounding box min longitude (default: Turkey west)")
    ap.add_argument("--lon_max",     type=float, default=45.0,
                    help="Bounding box max longitude (default: Turkey east)")
    ap.add_argument("--cop_url",     default="",
                    help="POST directly to COP (e.g. http://localhost:8100). "
                         "If omitted, output is JSONL on stdout.")
    ap.add_argument("--api_key",     default="",
                    help="X-API-Key for COP ingest (when --cop_url is set)")
    args = ap.parse_args()

    cop_url = args.cop_url or ""
    print(f"[ais] source={args.source} "
          f"bbox=[{args.lat_min},{args.lon_min}→{args.lat_max},{args.lon_max}] "
          f"output={'COP HTTP' if cop_url else 'stdout'}",
          file=sys.stderr)

    count = 0

    if args.source == "aisstream":
        if not args.ais_api_key:
            print("[ais] ERROR: --ais_api_key required for --source aisstream", file=sys.stderr)
            print("[ais] Get a free key at https://aisstream.io", file=sys.stderr)
            sys.exit(1)
        for vessel in read_aisstream(args.ais_api_key,
                                     args.lat_min, args.lat_max,
                                     args.lon_min, args.lon_max):
            ev = make_track_event(vessel)
            _emit(ev, cop_url, args.api_key)
            count += 1
            if count % 50 == 0:
                print(f"[ais] {count} vessel tracks emitted", file=sys.stderr)
        return

    # NMEA sentence-based sources (tcp / serial / file)
    if args.source == "tcp":
        sentences = read_tcp(args.host, args.port)
    elif args.source == "serial":
        sentences = read_serial(args.serial_port, args.baud)
    else:
        sentences = read_file(args.file)

    for sentence in sentences:
        vessel = decode_nmea_sentence(sentence)
        if vessel is None:
            continue
        if not (args.lat_min <= vessel["lat"] <= args.lat_max):
            continue
        if not (args.lon_min <= vessel["lon"] <= args.lon_max):
            continue
        ev = make_track_event(vessel)
        _emit(ev, cop_url, args.api_key)
        count += 1
        if count % 50 == 0:
            print(f"[ais] {count} vessel tracks emitted", file=sys.stderr)


if __name__ == "__main__":
    main()
