"""
adapters/cot_adapter.py  —  Cursor-on-Target (CoT) / TAK adapter for NIZAM

Listens for CoT XML events from:
  --source udp     : UDP multicast (SA-broadcast, default port 4242)
  --source tcp     : TAK Server TCP stream (port 8087 / 8088)
  --source file    : CoT XML file / JSONL of raw CoT strings (for testing)

Also emits CoT SA shares back to the network so ATAK/WinTAK/iTAK clients
can see NIZAM tracks on their maps (--cot_output_host / --cot_output_port).

CoT type → NIZAM mapping
  a-f-*  friendly  threat_level=LOW
  a-h-*  hostile   threat_level=HIGH
  a-u-*  unknown   threat_level=MEDIUM
  a-n-*  neutral   threat_level=LOW
  *-A-*  air       classification=aircraft
  *-G-*  ground    classification=ground
  *-S-*  surface   classification=surface

Outputs track.update JSONL to stdout → pipe into cop_publisher.py
  OR direct HTTP POST to COP (--cop_url).

Usage:
  # UDP multicast on LAN (ATAK devices broadcasting SA)
  python adapters/cot_adapter.py --source udp \\
    | python agents/cop_publisher.py

  # TAK Server TCP feed
  python adapters/cot_adapter.py --source tcp \\
    --tcp_host takserver.local --tcp_port 8087 \\
    | python agents/cop_publisher.py

  # Direct HTTP POST + echo CoT SA back to ATAK clients
  python adapters/cot_adapter.py --source udp \\
    --cop_url http://localhost:8100 --api_key MY_KEY \\
    --cot_output_host 239.2.3.1 --cot_output_port 6969

  # File / regression tests
  python adapters/cot_adapter.py --source file --file tests/fixtures/cot_sample.xml
"""
from __future__ import annotations

import argparse
import json
import queue
import socket
import struct
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

try:
    from shared.utils import utc_now_iso
except ImportError:
    from datetime import datetime, timezone
    def utc_now_iso() -> str:  # type: ignore[misc]
        return datetime.now(timezone.utc).isoformat()


# ── CoT type classification ────────────────────────────────────────────────────

def cot_type_to_fields(cot_type: str) -> Tuple[str, str, str]:
    """
    Map CoT type string → (affiliation, domain, intent).

    CoT type schema: affiliation-battle_dimension-function...
      a   = Assumed friend / atom
      f   = Friend
      h   = Hostile
      u   = Unknown
      n   = Neutral
      j   = Joker (unknown suspected hostile)
      k   = Faker (friend suspected hostile)
      Battle dimension:
      A   = Air
      G   = Ground
      S   = Sea Surface
      U   = Sub-surface
      F   = SOF
    """
    parts = cot_type.split("-")
    affil = parts[1].lower() if len(parts) > 1 else "u"
    dim   = parts[2].upper() if len(parts) > 2 else "A"

    threat = {
        "f": "LOW",
        "a": "LOW",   # assumed friend
        "n": "LOW",
        "u": "MEDIUM",
        "j": "HIGH",  # joker
        "k": "HIGH",  # faker
        "h": "HIGH",
    }.get(affil, "MEDIUM")

    classification = {
        "A": "aircraft",
        "G": "ground_vehicle",
        "S": "surface_vessel",
        "U": "subsurface",
        "F": "sof",
    }.get(dim, "unknown")

    intent = {
        "h": "attack",
        "j": "attack",
        "k": "attack",
        "f": "friendly",
        "a": "friendly",
        "n": "transit",
        "u": "unknown",
    }.get(affil, "unknown")

    return threat, classification, intent


# ── CoT XML parser ─────────────────────────────────────────────────────────────

def parse_cot_xml(xml_str: str) -> Optional[Dict[str, Any]]:
    """
    Parse a single CoT XML <event> string into an internal track dict.

    Returns None if the XML is malformed or not a position report.
    """
    try:
        root = ET.fromstring(xml_str.strip())
    except ET.ParseError:
        return None

    if root.tag != "event":
        return None

    cot_type = root.attrib.get("type", "a-u-A")
    uid      = root.attrib.get("uid", str(uuid.uuid4()))
    time_str = root.attrib.get("time", utc_now_iso())
    stale_str= root.attrib.get("stale", "")
    how      = root.attrib.get("how", "m-g")    # machine-generated / GPS

    # <point lat lon hae ce le>
    point = root.find("point")
    if point is None:
        return None
    try:
        lat = float(point.attrib["lat"])
        lon = float(point.attrib["lon"])
    except (KeyError, ValueError):
        return None

    hae   = float(point.attrib.get("hae", "0") or "0")   # height above ellipsoid (m)
    ce    = float(point.attrib.get("ce", "9999999") or "9999999")   # circular error (m)

    # <detail> children
    detail_el = root.find("detail")
    detail    = detail_el if detail_el is not None else ET.Element("detail")
    contact = detail.find("contact")
    track_d = detail.find("track")
    remarks = detail.find("remarks")
    uid_el  = detail.find("uid")

    callsign = ""
    if contact is not None:
        callsign = contact.attrib.get("callsign", "")
    if not callsign and uid_el is not None:
        callsign = uid_el.attrib.get("Droid", "")

    speed_mps  = 0.0
    heading_deg= 0.0
    if track_d is not None:
        try:
            speed_mps   = float(track_d.attrib.get("speed",  "0") or "0")
            heading_deg = float(track_d.attrib.get("course", "0") or "0")
        except ValueError:
            pass

    note = remarks.text.strip() if remarks is not None and remarks.text else ""

    threat_level, classification, intent = cot_type_to_fields(cot_type)

    # Accuracy: CE < 50 m → high confidence
    conf = 0.95 if ce < 50 else (0.75 if ce < 500 else 0.5)

    return {
        "uid":           uid,
        "cot_type":      cot_type,
        "callsign":      callsign,
        "lat":           lat,
        "lon":           lon,
        "altitude_m":    hae,
        "speed_mps":     speed_mps,
        "heading_deg":   heading_deg,
        "threat_level":  threat_level,
        "classification":classification,
        "intent":        intent,
        "confidence":    conf,
        "how":           how,
        "time":          time_str,
        "stale":         stale_str,
        "note":          note,
    }


def make_track_event(parsed: Dict[str, Any], source_label: str = "cot") -> Dict[str, Any]:
    """Convert parsed CoT dict → NIZAM track.update event."""
    uid        = parsed["uid"]
    track_id   = f"COT-{uid}"
    callsign   = parsed["callsign"] or uid[:8]

    return {
        "schema_version": "1.1",
        "event_id":       str(uuid.uuid4()),
        "event_type":     "track.update",
        "timestamp":      utc_now_iso(),
        "source": {
            "agent_id":    "cot-adapter",
            "instance_id": source_label,
            "host":        "local",
        },
        "correlation_id": track_id,
        "payload": {
            "global_track_id": track_id,
            "id":              track_id,
            "lat":             round(parsed["lat"],  7),
            "lon":             round(parsed["lon"],  7),
            "status":          "CONFIRMED",
            "classification": {
                "label":      parsed["classification"],
                "confidence": parsed["confidence"],
                "callsign":   callsign,
                "cot_type":   parsed["cot_type"],
            },
            "supporting_sensors": ["cot"],
            "kinematics": {
                "speed_mps":    round(parsed["speed_mps"],   2),
                "heading_deg":  round(parsed["heading_deg"], 1),
                "altitude_m":   round(parsed["altitude_m"],  1),
                "vertical_rate_mps": 0.0,
            },
            "intent":       parsed["intent"],
            "intent_conf":  parsed["confidence"],
            "threat_level": parsed["threat_level"],
            "threat_score": {"HIGH": 0.85, "MEDIUM": 0.45, "LOW": 0.1}.get(
                parsed["threat_level"], 0.1),
            "note":         parsed["note"],
            "history":      [],
        },
    }


# ── CoT output (SA broadcast back to ATAK clients) ────────────────────────────

def build_cot_xml(parsed: Dict[str, Any]) -> str:
    """Render a minimal CoT <event> XML string from a parsed track dict."""
    now    = utc_now_iso()
    stale  = parsed.get("stale") or now  # reuse original stale or use now
    callsign = parsed.get("callsign") or parsed["uid"][:8]

    return (
        f'<?xml version="1.0" standalone="yes"?>'
        f'<event version="2.0" uid="{_esc(parsed["uid"])}"'
        f' type="{_esc(parsed["cot_type"])}"'
        f' time="{_esc(now)}" start="{_esc(now)}" stale="{_esc(stale)}"'
        f' how="m-g">'
        f'<point lat="{parsed["lat"]:.7f}" lon="{parsed["lon"]:.7f}"'
        f' hae="{parsed["altitude_m"]:.1f}" ce="9999999" le="9999999"/>'
        f'<detail>'
        f'<contact callsign="{_esc(callsign)}"/>'
        f'<track speed="{parsed["speed_mps"]:.2f}"'
        f' course="{parsed["heading_deg"]:.1f}"/>'
        f'<uid Droid="{_esc(callsign)}"/>'
        f'</detail>'
        f'</event>'
    )


def _esc(s: str) -> str:
    """Minimal XML attribute escaping."""
    return (str(s)
            .replace("&", "&amp;")
            .replace('"', "&quot;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


class CotOutputSocket:
    """
    Sends CoT XML packets via UDP unicast or multicast.
    Used to echo NIZAM track state back to ATAK clients.
    """

    def __init__(self, host: str, port: int, multicast: bool = False) -> None:
        self.host      = host
        self.port      = port
        self.multicast = multicast
        self._sock     = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if multicast:
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 5)

    def send(self, parsed: Dict[str, Any]) -> None:
        xml = build_cot_xml(parsed).encode("utf-8")
        try:
            self._sock.sendto(xml, (self.host, self.port))
        except OSError as exc:
            print(f"[cot_out] send error: {exc}", file=sys.stderr)

    def close(self) -> None:
        self._sock.close()


# ── UDP multicast listener ─────────────────────────────────────────────────────

_MCAST_GROUP_DEFAULT = "239.2.3.1"   # SA multicast address used by ATAK by default
_UDP_PORT_DEFAULT    = 4242


def _udp_listen(
    mcast_group: str,
    port: int,
    out_q: "queue.Queue[str]",
    stop_event: threading.Event,
) -> None:
    """
    Background thread: join UDP multicast group and push raw XML strings
    into out_q.  Also accepts plain unicast UDP on the same port.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)  # Linux
    except AttributeError:
        pass
    sock.bind(("", port))

    # Join multicast group
    try:
        mreq = struct.pack("4sL", socket.inet_aton(mcast_group), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        print(f"[cot] UDP multicast joined {mcast_group}:{port}", file=sys.stderr)
    except OSError as exc:
        print(f"[cot] multicast join failed ({exc}), listening unicast UDP:{port}",
              file=sys.stderr)

    sock.settimeout(1.0)
    while not stop_event.is_set():
        try:
            data, _addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        raw = data.decode("utf-8", errors="replace")
        out_q.put(raw)

    sock.close()


# ── TCP client (TAK Server) ────────────────────────────────────────────────────

_TAK_TCP_PORT_DEFAULT = 8087   # TAK Server unencrypted stream
_TAK_XML_PROTO_MAGIC  = b"<?xml"


def _tcp_listen(
    host: str,
    port: int,
    out_q: "queue.Queue[str]",
    stop_event: threading.Event,
    reconnect_interval: float = 5.0,
) -> None:
    """
    Background thread: connect to TAK Server TCP, read a framed CoT stream.
    TAK Server uses a simple protocol: XML <event> elements separated by
    null bytes or newlines depending on version.  We buffer until we find
    a complete </event> tag.
    """
    while not stop_event.is_set():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10.0)
            sock.connect((host, port))
            print(f"[cot] TCP connected to {host}:{port}", file=sys.stderr)
            buf = b""
            while not stop_event.is_set():
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                # Split on </event>
                while b"</event>" in buf:
                    idx  = buf.index(b"</event>") + len(b"</event>")
                    raw  = buf[:idx]
                    buf  = buf[idx:]
                    # Find start of <event
                    start = raw.find(b"<event")
                    if start != -1:
                        out_q.put(raw[start:].decode("utf-8", errors="replace"))
            sock.close()
        except (OSError, ConnectionRefusedError) as exc:
            print(f"[cot] TCP {host}:{port} error: {exc} — reconnect in {reconnect_interval}s",
                  file=sys.stderr)

        if not stop_event.is_set():
            time.sleep(reconnect_interval)


# ── File source ────────────────────────────────────────────────────────────────

def _read_file_source(path: str) -> List[str]:
    """
    Read CoT XML events from a file.  Supports:
      - Single <event>...</event> document
      - Multiple <event> elements in one file (concatenated)
      - JSONL where each line has a "xml" or "raw" key
    """
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read().strip()
    except OSError as exc:
        print(f"[cot] file read error: {exc}", file=sys.stderr)
        return []

    if content.startswith("{"):
        # JSONL format
        events = []
        for line in content.splitlines():
            try:
                obj = json.loads(line)
                events.append(obj.get("xml") or obj.get("raw") or "")
            except json.JSONDecodeError:
                pass
        return [e for e in events if e]

    # Raw XML: split on </event>
    events = []
    rest = content
    while "<event" in rest:
        start = rest.find("<event")
        end   = rest.find("</event>", start)
        if end == -1:
            break
        events.append(rest[start: end + len("</event>")])
        rest = rest[end + len("</event>"):]
    return events


# ── HTTP output helper ─────────────────────────────────────────────────────────

class OutputHandler:
    """Emit NIZAM track.update events either to stdout or via HTTP POST."""

    def __init__(self, cop_url: Optional[str] = None, api_key: Optional[str] = None) -> None:
        self.cop_url = cop_url.rstrip("/") if cop_url else None
        self.api_key = api_key

    def emit(self, ev: Dict[str, Any]) -> None:
        line = json.dumps(ev, ensure_ascii=False)
        if not self.cop_url:
            print(line, flush=True)
            return
        data = line.encode()
        import urllib.request
        req = urllib.request.Request(
            f"{self.cop_url}/api/ingest",
            data=data,
            headers={
                "Content-Type":  "application/json",
                "User-Agent":    "NIZAM-CoT-adapter/1.0",
                **({"X-API-Key": self.api_key} if self.api_key else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception as exc:
            print(f"[cot] HTTP ingest error: {exc}", file=sys.stderr)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="CoT/TAK → NIZAM track.update adapter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--source", choices=["udp", "tcp", "file"],
                    default="udp",
                    help="Input source (default: udp)")
    # UDP options
    ap.add_argument("--mcast_group", default=_MCAST_GROUP_DEFAULT,
                    help=f"UDP multicast group (default: {_MCAST_GROUP_DEFAULT})")
    ap.add_argument("--udp_port",   type=int, default=_UDP_PORT_DEFAULT,
                    help=f"UDP listen port (default: {_UDP_PORT_DEFAULT})")
    # TCP options
    ap.add_argument("--tcp_host", default="localhost",
                    help="TAK Server hostname (--source tcp)")
    ap.add_argument("--tcp_port", type=int, default=_TAK_TCP_PORT_DEFAULT,
                    help=f"TAK Server TCP port (default: {_TAK_TCP_PORT_DEFAULT})")
    # File option
    ap.add_argument("--file", default="",
                    help="Path to CoT XML file (--source file)")
    # Output options
    ap.add_argument("--cop_url",  default="",
                    help="Direct HTTP POST to COP (e.g. http://localhost:8100)")
    ap.add_argument("--api_key",  default="",
                    help="X-API-Key for /api/ingest (if AUTH_ENABLED)")
    # CoT SA output (echo back to ATAK clients)
    ap.add_argument("--cot_output_host", default="",
                    help="Host/group to echo CoT SA output to (ATAK clients)")
    ap.add_argument("--cot_output_port", type=int, default=6969,
                    help="Port for CoT SA output (default: 6969)")
    ap.add_argument("--cot_output_mcast", action="store_true",
                    help="Use UDP multicast for CoT output")

    args = ap.parse_args()

    out    = OutputHandler(args.cop_url or None, args.api_key or None)
    cot_tx = None
    if args.cot_output_host:
        cot_tx = CotOutputSocket(
            args.cot_output_host, args.cot_output_port, args.cot_output_mcast
        )
        print(f"[cot] SA output → {args.cot_output_host}:{args.cot_output_port}",
              file=sys.stderr)

    def handle(raw_xml: str) -> None:
        parsed = parse_cot_xml(raw_xml)
        if parsed is None:
            return
        ev = make_track_event(parsed, source_label=args.source)
        out.emit(ev)
        if cot_tx:
            cot_tx.send(parsed)

    if args.source == "file":
        events = _read_file_source(args.file)
        print(f"[cot] file: processing {len(events)} CoT events", file=sys.stderr)
        for raw in events:
            handle(raw)
        return

    # Live sources — use a worker queue + background listener thread
    q: "queue.Queue[str]" = queue.Queue(maxsize=2000)
    stop = threading.Event()

    if args.source == "udp":
        t = threading.Thread(
            target=_udp_listen,
            args=(args.mcast_group, args.udp_port, q, stop),
            daemon=True,
        )
    else:  # tcp
        t = threading.Thread(
            target=_tcp_listen,
            args=(args.tcp_host, args.tcp_port, q, stop),
            daemon=True,
        )
    t.start()
    print(f"[cot] source={args.source} running (Ctrl-C to stop)", file=sys.stderr)

    try:
        while True:
            try:
                raw = q.get(timeout=1.0)
            except queue.Empty:
                continue
            handle(raw)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if cot_tx:
            cot_tx.close()


if __name__ == "__main__":
    main()
