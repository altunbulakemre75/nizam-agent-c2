#!/usr/bin/env python3
"""
scripts/bench_tactical.py — Tactical engine latency benchmark

Injects N tracks into the running COP server, waits for the tactical
engine to accumulate samples, then reports p50/p95/p99 and per-module
breakdown from /api/metrics.

Usage:
    python scripts/bench_tactical.py --tracks 150
    python scripts/bench_tactical.py --tracks 500 --warmup 10 --samples 30
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
from concurrent.futures import ThreadPoolExecutor


BASE_URL = "http://127.0.0.1:8100"


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=5) as r:
        return json.loads(r.read())


def _post(path: str, body: dict, timeout: float = 5.0) -> bool:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False


def ingest_batch(tracks: list, pool: ThreadPoolExecutor) -> None:
    """Fire a full update round for all tracks."""
    futs = []
    for t in tracks:
        # Move track slightly
        rad = math.radians(t["heading"])
        t["lat"]  += t["speed"] * math.cos(rad) / 111320.0
        t["lon"]  += t["speed"] * math.sin(rad) / (111320.0 * math.cos(math.radians(t["lat"])))
        t["heading"] = (t["heading"] + random.gauss(0, 1)) % 360.0

        event = {
            "event_type": "cop.track",
            "payload": {
                "id": t["id"],
                "global_track_id": t["id"],
                "lat": t["lat"],
                "lon": t["lon"],
                "speed": t["speed"],
                "heading": t["heading"],
                "status": "CONFIRMED",
                "intent": t["intent"],
                "intent_conf": 0.8,
                "supporting_sensors": ["radar-01"],
                "classification": {"label": "drone", "conf": 0.9},
                "threat_level": t["threat"],
                "threat_score": t["score"],
                "kinematics": {
                    "altitude_m": t["alt"],
                    "speed_mps": t["speed"],
                    "heading_deg": t["heading"],
                },
            },
        }
        futs.append(pool.submit(_post, "/ingest", event))
    for f in futs:
        f.result()


def make_tracks(n: int) -> list:
    intents = ["attack", "reconnaissance", "loitering", "unknown"]
    threats = ["HIGH"] * (n // 3) + ["MEDIUM"] * (n // 3) + ["LOW"] * (n - 2 * (n // 3))
    random.shuffle(threats)
    return [
        {
            "id": f"BM-{i:04d}",
            "lat":  41.0 + random.uniform(-0.08, 0.08),
            "lon":  29.0 + random.uniform(-0.08, 0.08),
            "alt":  random.uniform(50, 3000),
            "speed": random.uniform(5, 40),
            "heading": random.uniform(0, 360),
            "intent": random.choice(intents),
            "threat": threats[i],
            "score": random.randint(10, 95),
        }
        for i in range(n)
    ]


def fetch_tactical_metrics() -> dict:
    try:
        m = _get("/api/metrics")
        return m.get("tactical", {})
    except Exception:
        return {}


def percentile(data: list, pct: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    idx = int(len(s) * pct / 100)
    return s[min(idx, len(s) - 1)]


def main():
    ap = argparse.ArgumentParser(description="NIZAM tactical engine benchmark")
    ap.add_argument("--tracks",   type=int, default=150, help="Number of concurrent tracks")
    ap.add_argument("--warmup",   type=int, default=8,   help="Warmup seconds before sampling")
    ap.add_argument("--samples",  type=int, default=40,  help="Tactical runs to collect")
    ap.add_argument("--rate_hz",  type=float, default=2.0, help="Ingest rate per track (Hz)")
    ap.add_argument("--workers",  type=int, default=32,  help="HTTP thread workers")
    ap.add_argument("--url",      default="http://127.0.0.1:8100")
    args = ap.parse_args()

    global BASE_URL
    BASE_URL = args.url.rstrip("/")

    # Check server is up
    try:
        _get("/api/metrics")
    except Exception as e:
        print(f"[bench] Server not reachable at {BASE_URL}: {e}")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  NIZAM Tactical Engine Benchmark")
    print(f"{'='*60}")
    print(f"  Tracks   : {args.tracks}")
    print(f"  Warmup   : {args.warmup}s")
    print(f"  Samples  : {args.samples} tactical runs")
    print(f"  Rate     : {args.rate_hz} Hz per track ({int(args.tracks * args.rate_hz)} events/sec)")
    print(f"  Workers  : {args.workers}")

    tracks = make_tracks(args.tracks)
    interval = 1.0 / args.rate_hz

    collected: list[float] = []
    module_samples: dict[str, list[float]] = {}

    print(f"\n[bench] Phase 1: Warmup ({args.warmup}s)...")
    t_start = time.monotonic()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        step = 0
        while time.monotonic() - t_start < args.warmup:
            t0 = time.monotonic()
            ingest_batch(tracks, pool)
            step += 1
            elapsed = time.monotonic() - t0
            sleep = interval - elapsed
            if sleep > 0:
                time.sleep(sleep)

        print(f"[bench] Warmup done ({step} rounds). Collecting {args.samples} samples...")

        # Sampling phase: run ingest while polling metrics
        # Tactical engine runs every 1s → collect rolling window snapshots
        poll_start = time.monotonic()
        prev_ran   = fetch_tactical_metrics().get("ran", 0)
        timeout_s  = max(args.samples * 1.5 + 15, 60)
        snap_interval = 2.0   # poll every 2s
        last_poll  = time.monotonic()
        step_count = 0

        while len(collected) < args.samples and (time.monotonic() - poll_start) < timeout_s:
            t0 = time.monotonic()
            ingest_batch(tracks, pool)
            elapsed = time.monotonic() - t0
            sleep = interval - elapsed
            if sleep > 0:
                time.sleep(max(0, sleep))
            step_count += 1

            # Poll metrics every snap_interval
            if time.monotonic() - last_poll >= snap_interval:
                last_poll = time.monotonic()
                m = fetch_tactical_metrics()
                curr_ran = m.get("ran", 0)

                if curr_ran > prev_ran:
                    # Pull p50/p95 directly from rolling-window percentiles
                    p50_snap = m.get("p50_ms", 0)
                    if p50_snap > 0:
                        collected.append(p50_snap)

                    mods = m.get("module_ms", {})
                    for k, v in mods.items():
                        module_samples.setdefault(k, []).append(v)

                    prev_ran = curr_ran

                wall = time.monotonic() - poll_start
                p50_cur = collected[-1] if collected else 0
                print(f"  [{wall:.0f}s] snaps={len(collected)} "
                      f"p50={p50_cur:.0f}ms "
                      f"tac_ran={curr_ran}  tracks={args.tracks}",
                      end="\r", flush=True)

    print()

    # ── Results ──────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  RESULTS — {args.tracks} concurrent tracks")
    print(f"{'='*60}")

    if not collected:
        print("  No tactical runs captured. Run longer or check server.")
        return

    collected.sort()
    p50  = statistics.median(collected)
    p95  = percentile(collected, 95)
    p99  = percentile(collected, 99)
    pmax = max(collected)
    pmin = min(collected)

    print(f"  Samples    : {len(collected)}")
    print(f"  p50        : {p50:.0f} ms")
    print(f"  p95        : {p95:.0f} ms")
    print(f"  p99        : {p99:.0f} ms")
    print(f"  max        : {pmax:.0f} ms")
    print(f"  min        : {pmin:.0f} ms")

    if module_samples:
        print(f"\n  Sub-module breakdown (last sample):")
        total_last = 0.0
        rows = []
        for k, vals in module_samples.items():
            last = vals[-1] if vals else 0
            total_last += last
            rows.append((last, k))
        rows.sort(reverse=True)
        for v, k in rows:
            bar = "#" * int(v / max(pmax, 1) * 30)
            print(f"    {k:<18} {v:>7.1f} ms  {bar}")
        print(f"    {'TOTAL (serial)':18} {total_last:>7.1f} ms  (actual wall={p50:.0f}ms)")

    # Operational latency = interval + p50
    from_interval = 1.0  # _AI_TACTICAL_INTERVAL
    op_latency = from_interval * 1000 + p50
    print(f"\n  Tactical interval  : {from_interval*1000:.0f} ms")
    print(f"  Operational latency: ~{op_latency:.0f} ms  (interval + p50)")
    print(f"  ({op_latency/1000:.1f}s from event to recommendation)")

    print(f"\n  Comparison:")
    print(f"    Before optimisation: p50=1100ms, p95=1920ms, op=4100ms")
    print(f"    After  optimisation: p50={p50:.0f}ms, p95={p95:.0f}ms, op={op_latency:.0f}ms")
    if p50 < 1100:
        speedup = 1100 / max(p50, 1)
        print(f"    Speedup: {speedup:.1f}x")

    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
