"""
scripts/load_test.py — 1000+ track load test for NIZAM COP

Sends concurrent track + threat events to /ingest and measures:
  - Throughput (events/sec)
  - Latency (p50, p95, p99)
  - Error rate
  - Memory / track count at end

Usage:
  # Start the COP server first, then:
  python scripts/load_test.py --tracks 1000 --duration 30
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List


def make_track_event(track_id: str, lat: float, lon: float,
                     speed: float, heading: float, step: int) -> dict:
    return {
        "event_type": "cop.track",
        "payload": {
            "id": track_id,
            "global_track_id": track_id,
            "lat": lat,
            "lon": lon,
            "speed": round(speed, 2),
            "heading": round(heading, 2),
            "status": "CONFIRMED",
            "intent": random.choice(["attack", "reconnaissance", "loitering", "unknown"]),
            "intent_conf": round(random.uniform(0.3, 0.95), 3),
            "supporting_sensors": ["radar-01", "rf-01"],
            "classification": {"label": "drone", "conf": 0.85},
            "threat_level": random.choice(["HIGH", "MEDIUM", "LOW"]),
            "threat_score": random.randint(10, 95),
        },
    }


def post_event(url: str, event: dict, timeout: float = 5.0) -> tuple:
    """POST a single event, return (success, latency_ms, status_code)."""
    data = json.dumps(event).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            latency = (time.monotonic() - t0) * 1000
            return True, latency, resp.status
    except urllib.error.HTTPError as e:
        latency = (time.monotonic() - t0) * 1000
        return False, latency, e.code
    except Exception:
        latency = (time.monotonic() - t0) * 1000
        return False, latency, 0


def main():
    ap = argparse.ArgumentParser(description="NIZAM COP load test")
    ap.add_argument("--url", default="http://127.0.0.1:8100/ingest")
    ap.add_argument("--tracks", type=int, default=1000, help="Number of concurrent tracks")
    ap.add_argument("--duration", type=int, default=30, help="Test duration in seconds")
    ap.add_argument("--rate_hz", type=float, default=2.0, help="Updates per track per second")
    ap.add_argument("--workers", type=int, default=32, help="Thread pool size")
    args = ap.parse_args()

    print(f"[load_test] {args.tracks} tracks, {args.duration}s, {args.rate_hz} Hz, {args.workers} workers")
    print(f"[load_test] Target: {args.url}")
    print(f"[load_test] Expected throughput: {args.tracks * args.rate_hz:.0f} events/sec")
    print()

    # Generate track starting positions (random around Istanbul)
    tracks = []
    for i in range(args.tracks):
        lat = 41.0 + random.uniform(-0.05, 0.05)
        lon = 29.0 + random.uniform(-0.05, 0.05)
        speed = random.uniform(5, 50)
        heading = random.uniform(0, 360)
        tracks.append({
            "id": f"LT-{i:04d}",
            "lat": lat, "lon": lon,
            "speed": speed, "heading": heading,
        })

    latencies: List[float] = []
    errors = 0
    total_sent = 0
    rate_limited = 0

    interval = 1.0 / args.rate_hz
    start_time = time.monotonic()
    step = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        while time.monotonic() - start_time < args.duration:
            step += 1
            batch_start = time.monotonic()

            # Move all tracks
            futures = []
            for t in tracks:
                hdg_rad = math.radians(t["heading"])
                dt = interval
                dlat = t["speed"] * math.cos(hdg_rad) * dt / 111320.0
                dlon = t["speed"] * math.sin(hdg_rad) * dt / (111320.0 * math.cos(math.radians(t["lat"])))
                t["lat"] += dlat
                t["lon"] += dlon
                # Small heading variation
                t["heading"] = (t["heading"] + random.gauss(0, 2)) % 360

                event = make_track_event(t["id"], t["lat"], t["lon"], t["speed"], t["heading"], step)
                futures.append(pool.submit(post_event, args.url, event))

            for f in as_completed(futures):
                ok, lat_ms, code = f.result()
                total_sent += 1
                latencies.append(lat_ms)
                if not ok:
                    errors += 1
                    if code == 429:
                        rate_limited += 1

            elapsed = time.monotonic() - batch_start
            sleep_time = interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

            # Progress every 5 steps
            if step % (int(5 * args.rate_hz)) == 0:
                wall = time.monotonic() - start_time
                rps = total_sent / wall if wall > 0 else 0
                p50 = statistics.median(latencies[-1000:]) if latencies else 0
                print(f"  [{wall:.0f}s] sent={total_sent} err={errors} "
                      f"rps={rps:.0f} p50={p50:.1f}ms", flush=True)

    wall_total = time.monotonic() - start_time

    # Final stats
    print()
    print("=" * 60)
    print(f"  Duration:      {wall_total:.1f}s")
    print(f"  Total sent:    {total_sent}")
    print(f"  Errors:        {errors} ({errors/max(total_sent,1)*100:.1f}%)")
    print(f"  Rate limited:  {rate_limited}")
    print(f"  Throughput:    {total_sent/wall_total:.0f} events/sec")

    if latencies:
        latencies.sort()
        print(f"  Latency p50:   {statistics.median(latencies):.1f} ms")
        print(f"  Latency p95:   {latencies[int(len(latencies)*0.95)]:.1f} ms")
        print(f"  Latency p99:   {latencies[int(len(latencies)*0.99)]:.1f} ms")
        print(f"  Latency max:   {max(latencies):.1f} ms")

    print("=" * 60)

    # Check server state
    try:
        with urllib.request.urlopen(f"{args.url.replace('/ingest', '/api/metrics')}", timeout=5) as r:
            metrics = json.loads(r.read())
            print(f"\n  Server tracks:  {metrics.get('tracks', '?')}")
            print(f"  Server ingest:  {metrics.get('ingest_total', '?')}")
    except Exception:
        pass

    # Exit code: 1 if error rate > 5%
    if errors / max(total_sent, 1) > 0.05:
        print("\n  FAIL: error rate > 5%")
        sys.exit(1)
    else:
        print("\n  PASS")


if __name__ == "__main__":
    main()
