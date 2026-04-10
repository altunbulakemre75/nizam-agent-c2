#!/usr/bin/env python3
"""
scripts/cot_test_sender.py — Simulate ATAK device CoT SA broadcast

Sends Cursor-on-Target (CoT) XML messages via UDP multicast (or unicast)
to simulate ATAK / WinTAK / iTAK devices transmitting Situational Awareness
position reports to the NIZAM cot_adapter.

Default target: UDP multicast 239.2.3.1:4242  (ATAK SA broadcast standard)

Usage:
    python scripts/cot_test_sender.py
    python scripts/cot_test_sender.py --count 20 --interval 0.5
    python scripts/cot_test_sender.py --unicast --host 127.0.0.1 --port 4242
    python scripts/cot_test_sender.py --scenario swarm --count 50
"""
from __future__ import annotations

import argparse
import math
import random
import socket
import struct
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple


# ── CoT XML helpers ──────────────────────────────────────────────────────────

def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _stale(ts: float, stale_s: float = 60.0) -> str:
    return _iso(ts + stale_s)


def _uid(prefix: str, idx: int) -> str:
    return f"{prefix}-{idx:04d}"


COT_TYPES = {
    "friendly_air":    "a-f-A-C-F",
    "hostile_ground":  "a-h-G-U-C",
    "unknown_air":     "a-u-A",
    "joker":           "a-j-A",
    "friendly_ground": "a-f-G-U-C",
}


def build_cot_xml(
    uid: str,
    lat: float,
    lon: float,
    alt_m: float = 0.0,
    speed_mps: float = 0.0,
    heading_deg: float = 0.0,
    cot_type: str = "a-h-G-U-C",
    callsign: str = "HOSTILE",
    hae: float = 0.0,
    ce: float = 50.0,
    le: float = 50.0,
) -> bytes:
    """Return a minimal but valid CoT XML SA event as UTF-8 bytes."""
    now = time.time()
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<event version="2.0" uid="{uid}" type="{cot_type}"'
        f' time="{_iso(now)}" start="{_iso(now)}" stale="{_stale(now)}"'
        f' how="m-g">'
        f'<point lat="{lat:.6f}" lon="{lon:.6f}" hae="{hae:.1f}"'
        f' ce="{ce:.0f}" le="{le:.0f}"/>'
        f'<detail>'
        f'<contact callsign="{callsign}"/>'
        f'<track speed="{speed_mps:.2f}" course="{heading_deg:.1f}"/>'
        f'<altitude value="{alt_m:.1f}"/>'
        f'</detail>'
        f'</event>'
    )
    return xml.encode("utf-8")


# ── Track state machine ──────────────────────────────────────────────────────

class SimTrack:
    def __init__(
        self,
        uid: str,
        lat: float,
        lon: float,
        alt_m: float,
        speed_mps: float,
        heading_deg: float,
        cot_type: str,
        callsign: str,
    ) -> None:
        self.uid         = uid
        self.lat         = lat
        self.lon         = lon
        self.alt_m       = alt_m
        self.speed_mps   = speed_mps
        self.heading_deg = heading_deg
        self.cot_type    = cot_type
        self.callsign    = callsign

    def step(self, dt: float) -> None:
        """Advance position by dt seconds at current speed/heading."""
        dist_m = self.speed_mps * dt
        rad    = math.radians(self.heading_deg)
        dlat   = (dist_m * math.cos(rad)) / 111_320.0
        dlon   = (dist_m * math.sin(rad)) / (111_320.0 * math.cos(math.radians(self.lat)))
        self.lat         += dlat
        self.lon         += dlon
        # Slight random heading drift
        self.heading_deg  = (self.heading_deg + random.gauss(0, 0.5)) % 360.0
        self.alt_m        = max(0.0, self.alt_m + random.gauss(0, 1.0))

    def to_xml(self) -> bytes:
        return build_cot_xml(
            uid=self.uid,
            lat=self.lat,
            lon=self.lon,
            alt_m=self.alt_m,
            speed_mps=self.speed_mps,
            heading_deg=self.heading_deg,
            cot_type=self.cot_type,
            callsign=self.callsign,
            hae=self.alt_m,
        )


# ── Scenario factories ───────────────────────────────────────────────────────

# Istanbul area default origin
DEFAULT_LAT = 41.015
DEFAULT_LON = 28.979


def scenario_single(n: int) -> List[SimTrack]:
    """One hostile drone approaching from the north."""
    return [SimTrack(
        uid=_uid("ATAK-HOSTILE", 1),
        lat=DEFAULT_LAT + 0.05,
        lon=DEFAULT_LON,
        alt_m=120.0,
        speed_mps=15.0,
        heading_deg=180.0,   # south
        cot_type=COT_TYPES["hostile_ground"],
        callsign="HOSTILE-01",
    )]


def scenario_swarm(n: int) -> List[SimTrack]:
    """Swarm of hostile UAS approaching from multiple directions."""
    tracks = []
    headings = [135, 150, 165, 180, 195, 210, 225]
    for i in range(min(n, len(headings))):
        offset_lat = random.uniform(0.02, 0.06)
        offset_lon = random.uniform(-0.03, 0.03)
        tracks.append(SimTrack(
            uid=_uid("SWARM", i + 1),
            lat=DEFAULT_LAT + offset_lat,
            lon=DEFAULT_LON + offset_lon,
            alt_m=random.uniform(50, 200),
            speed_mps=random.uniform(10, 25),
            heading_deg=headings[i],
            cot_type=COT_TYPES["hostile_ground"],
            callsign=f"SWARM-{i+1:02d}",
        ))
    return tracks


def scenario_mixed(n: int) -> List[SimTrack]:
    """Mix of friendly, hostile, and unknown tracks."""
    return [
        SimTrack(_uid("FRIENDLY", 1), DEFAULT_LAT - 0.01, DEFAULT_LON - 0.02,
                 500.0, 80.0, 90.0, COT_TYPES["friendly_air"], "BLUFOR-01"),
        SimTrack(_uid("HOSTILE",  1), DEFAULT_LAT + 0.04, DEFAULT_LON + 0.01,
                 80.0, 18.0, 200.0, COT_TYPES["hostile_ground"], "OPFOR-01"),
        SimTrack(_uid("UNKNOWN",  1), DEFAULT_LAT + 0.02, DEFAULT_LON - 0.03,
                 200.0, 30.0, 160.0, COT_TYPES["unknown_air"], "UNK-01"),
        SimTrack(_uid("JOKER",    1), DEFAULT_LAT + 0.06, DEFAULT_LON + 0.02,
                 300.0, 50.0, 220.0, COT_TYPES["joker"], "JOKER-01"),
    ]


SCENARIOS = {
    "single": scenario_single,
    "swarm":  scenario_swarm,
    "mixed":  scenario_mixed,
}


# ── UDP socket helpers ───────────────────────────────────────────────────────

def make_multicast_socket(ttl: int = 1) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", ttl))
    return sock


def make_unicast_socket() -> socket.socket:
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="NIZAM CoT test sender — simulates ATAK device SA broadcast")
    p.add_argument("--host",     default="239.2.3.1", help="Destination host (default: 239.2.3.1 multicast)")
    p.add_argument("--port",     type=int, default=4242, help="UDP port (default: 4242)")
    p.add_argument("--unicast",  action="store_true", help="Use unicast instead of multicast")
    p.add_argument("--count",    type=int, default=0, help="Total messages to send (0 = infinite)")
    p.add_argument("--interval", type=float, default=1.0, help="Seconds between updates (default: 1.0)")
    p.add_argument("--scenario", choices=list(SCENARIOS.keys()), default="mixed",
                   help="Track scenario (default: mixed)")
    p.add_argument("--tracks",   type=int, default=7, help="Track count hint for swarm scenario")
    p.add_argument("--verbose",  action="store_true", help="Print each packet sent")
    args = p.parse_args()

    factory = SCENARIOS[args.scenario]
    tracks  = factory(args.tracks)

    sock = make_unicast_socket() if args.unicast else make_multicast_socket()
    dst  = (args.host, args.port)
    mode = "unicast" if args.unicast else "multicast"

    print(f"[cot_test_sender] Scenario: {args.scenario} | Tracks: {len(tracks)}")
    print(f"[cot_test_sender] Sending {mode} → {args.host}:{args.port} every {args.interval}s")
    print(f"[cot_test_sender] Count: {'∞' if args.count == 0 else args.count}")
    print("[cot_test_sender] Press Ctrl+C to stop\n")

    sent = 0
    try:
        while args.count == 0 or sent < args.count:
            for t in tracks:
                xml = t.to_xml()
                sock.sendto(xml, dst)
                if args.verbose:
                    print(f"  → {t.uid} lat={t.lat:.5f} lon={t.lon:.5f} alt={t.alt_m:.0f}m hdg={t.heading_deg:.0f}°")
                t.step(args.interval)

            sent += len(tracks)
            print(f"[cot_test_sender] sent={sent}", end="\r", flush=True)
            time.sleep(args.interval)

    except KeyboardInterrupt:
        print(f"\n[cot_test_sender] Stopped. Total messages sent: {sent}")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
