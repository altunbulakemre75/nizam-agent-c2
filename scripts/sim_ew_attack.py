"""
scripts/sim_ew_attack.py - Simulate EW attack patterns to exercise the UI

Injects three attack sequences against a live NIZAM COP server:

  Phase 1 - GPS SPOOFING
    Track T-SPOOF-01 placed at (41.00, 29.00) then immediately jumps
    ~50 km north (41.50, 29.00) within 0.8 s -> implies ~62,500 m/s.
    Triggers GPS_SPOOFING / CRITICAL alert.

  Phase 2 - FALSE INJECTION
    10 brand-new tracks all attributed to "sensor-attacker" within 3 s.
    Triggers FALSE_INJECTION / HIGH alert on the 9th track.

  Phase 3 - RADAR JAMMING
    Registers 5 tracks from "radar-sim", then stops sending updates.
    After JAMMING_STALE_S (8 s) the tactical engine's check_mass_jamming
    call fires RADAR_JAMMING / CRITICAL.  This script waits 12 s and then
    hits /api/tactical/force_check (if available) or just waits for the
    background task to detect it naturally.

Usage:
    python scripts/sim_ew_attack.py
    python scripts/sim_ew_attack.py --url http://127.0.0.1:8100 --delay 1.0
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def post_ingest(base_url: str, payload: dict, api_key: str = "") -> bool:
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(
        f"{base_url}/ingest", data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5):
            return True
    except urllib.error.HTTPError as e:
        print(f"    [!] HTTP {e.code}: {e.read().decode()[:120]}")
        return False
    except Exception as e:
        print(f"    [!] {e}")
        return False


def send_track(base_url: str, track_id: str, lat: float, lon: float,
               speed: float = 20.0, heading: float = 180.0,
               label: str = "drone", sensors: list | None = None,
               api_key: str = "") -> bool:
    payload = {
        "event_type":  "cop.track",
        "server_time": utc_now_iso(),
        "payload": {
            "id":       track_id,
            "lat":      lat,
            "lon":      lon,
            "speed":    speed,
            "heading":  heading,
            "label":    label,
            "sensors":  sensors or ["radar-sim"],
        },
    }
    return post_ingest(base_url, payload, api_key)


# ── Phase 1: GPS Spoofing ─────────────────────────────────────────────────

def phase_gps_spoofing(base_url: str, delay: float, api_key: str) -> None:
    print("\n[Phase 1] GPS SPOOFING")
    print("  -> Sending T-SPOOF-01 at (41.00, 29.00) ...")
    send_track(base_url, "T-SPOOF-01", 41.00, 29.00,
               sensors=["radar-sim"], api_key=api_key)
    time.sleep(min(delay, 0.8))

    print("  -> Jumping T-SPOOF-01 to (41.50, 29.00) - ~50 km in <1 s ...")
    send_track(base_url, "T-SPOOF-01", 41.50, 29.00,
               sensors=["radar-sim"], api_key=api_key)
    print("  [OK] GPS_SPOOFING alert should fire (>500 m/s implied speed)")


# ── Phase 2: False Injection ──────────────────────────────────────────────

def phase_false_injection(base_url: str, api_key: str) -> None:
    print("\n[Phase 2] FALSE INJECTION")
    n = 11  # INJECTION_RATE_THRESH is 8, so 9+ triggers the alert
    print(f"  -> Flooding {n} new tracks from 'sensor-attacker' in ~3 s ...")
    for i in range(n):
        tid = f"T-INJECT-{i:03d}"
        lat = 41.00 + i * 0.002
        send_track(base_url, tid, lat, 29.00,
                   sensors=["sensor-attacker"], api_key=api_key)
        print(f"    sent {tid} ({i+1}/{n})")
        time.sleep(3.0 / n)
    print("  [OK] FALSE_INJECTION / HIGH alert should have fired")


# ── Phase 3: Radar Jamming ────────────────────────────────────────────────

def phase_radar_jamming(base_url: str, api_key: str) -> None:
    print("\n[Phase 3] RADAR JAMMING (mass stale)")
    n = 6  # JAMMING_TRACK_COUNT is 4, need >= 5
    print(f"  -> Registering {n} tracks via radar-sim ...")
    for i in range(n):
        tid = f"T-JAM-{i:03d}"
        lat = 41.10 + i * 0.002
        send_track(base_url, tid, lat, 29.05,
                   sensors=["radar-sim"], api_key=api_key)
        time.sleep(0.1)
    print(f"  -> All {n} tracks registered. Stopping updates for 12 s ...")
    print("     (JAMMING_STALE_S=8 s - tactical background task will detect)")
    for remaining in range(12, 0, -1):
        print(f"\r     waiting {remaining:2d} s ...", end="", flush=True)
        time.sleep(1)
    print("\n  [OK] RADAR_JAMMING / CRITICAL alert should have fired")


# ── Main ──────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="EW attack simulation for NIZAM COP")
    ap.add_argument("--url",     default="http://127.0.0.1:8100")
    ap.add_argument("--delay",   type=float, default=1.0,
                    help="Inter-phase pause in seconds (default 1.0)")
    ap.add_argument("--api_key", default="", help="INGEST_API_KEY if auth enabled")
    args = ap.parse_args()

    print(f"NIZAM EW Attack Simulation -> {args.url}")
    print("Open the browser and watch the [EW] EW tab.\n")

    # Verify server is reachable
    try:
        urllib.request.urlopen(f"{args.url}/api/metrics", timeout=5)
    except Exception as e:
        print(f"[!] Cannot reach {args.url}: {e}")
        print("    Start the server first:  python start.py")
        return

    phase_gps_spoofing(args.url, args.delay, args.api_key)
    time.sleep(args.delay)

    phase_false_injection(args.url, args.api_key)
    time.sleep(args.delay)

    phase_radar_jamming(args.url, args.api_key)

    print("\n[Done] Check the [EW] EW tab in the browser - all three attack types should be visible.")


if __name__ == "__main__":
    main()
