"""
scripts/stress_test.py — Tactical engine stress / load test

Injects N synthetic tracks at a configurable rate and polls /api/metrics
to measure tactical engine latency (p50 / p95 / p99) at steady state.

Usage:
  python scripts/stress_test.py
  python scripts/stress_test.py --tracks 100 --rate 20 --duration 60
  python scripts/stress_test.py --url http://localhost:8100 --api_key KEY
  python scripts/stress_test.py --tracks 200 --rate 50 --duration 120 --workers 8

What it measures:
  • Ingest throughput (events / second accepted by the server)
  • Tactical engine latency percentiles (p50 / p95 / p99) as reported
    by /api/metrics after the warmup period
  • HTTP error rate (4xx / 5xx from /api/ingest)

Exit codes:
  0 — all thresholds passed (or --no_assert)
  1 — one or more thresholds breached
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ── Thresholds (pass / fail criteria) ────────────────────────────────────────
DEFAULT_P95_WARN_MS  = 2000   # p95 > 2s → warning
DEFAULT_P99_FAIL_MS  = 5000   # p99 > 5s → fail

# ── Track generation ──────────────────────────────────────────────────────────

_BASE_LAT = 41.0
_BASE_LON = 29.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_track_event(track_id: str, step: int) -> Dict[str, Any]:
    """Generate a synthetic track.update event near Istanbul."""
    angle  = (step * 3.7) % 360
    radius = 0.05 + 0.03 * math.sin(step * 0.1)
    lat    = round(_BASE_LAT + radius * math.cos(math.radians(angle)), 6)
    lon    = round(_BASE_LON + radius * math.sin(math.radians(angle)), 6)
    speed  = round(15.0 + 10.0 * random.random(), 1)

    return {
        "schema_version": "1.1",
        "event_id":       str(uuid.uuid4()),
        "event_type":     "track.update",
        "timestamp":      _utc_now_iso(),
        "source": {
            "agent_id": "stress-test",
            "instance_id": "stress-01",
            "host": "local",
        },
        "correlation_id": track_id,
        "payload": {
            "global_track_id": track_id,
            "id": track_id,
            "lat": lat,
            "lon": lon,
            "status": "CONFIRMED",
            "classification": {"label": "drone", "confidence": 0.85},
            "supporting_sensors": ["radar"],
            "kinematics": {
                "speed_mps":   speed,
                "heading_deg": angle,
                "altitude_m":  random.uniform(50, 800),
            },
            "intent":      random.choice(["unknown", "attack", "recon"]),
            "intent_conf": round(random.random(), 2),
            "threat_level": random.choice(["LOW", "MEDIUM", "HIGH"]),
            "threat_score": round(random.random(), 2),
        },
    }


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _post_event(
    url: str,
    event: Dict[str, Any],
    api_key: str,
    timeout: float = 5.0,
) -> int:
    """POST one event to /api/ingest. Returns HTTP status code."""
    data = json.dumps(event).encode()
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def _get_metrics(base_url: str, api_key: str) -> Optional[Dict[str, Any]]:
    headers: Dict[str, str] = {}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(f"{base_url}/api/metrics", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


# ── Worker ────────────────────────────────────────────────────────────────────

def _worker(
    track_ids: List[str],
    ingest_url: str,
    api_key: str,
    rate_hz: float,
    duration_s: float,
) -> Dict[str, int]:
    """Send updates for the given tracks at rate_hz for duration_s seconds."""
    interval = 1.0 / rate_hz if rate_hz > 0 else 0.0
    deadline = time.monotonic() + duration_s
    counters  = {"ok": 0, "err": 0}
    step      = 0

    while time.monotonic() < deadline:
        t0 = time.monotonic()
        for tid in track_ids:
            ev     = _make_track_event(tid, step)
            status = _post_event(ingest_url, ev, api_key)
            if 200 <= status < 300:
                counters["ok"] += 1
            else:
                counters["err"] += 1
        step += 1
        elapsed = time.monotonic() - t0
        sleep_s = interval - elapsed
        if sleep_s > 0:
            time.sleep(sleep_s)

    return counters


# ── Main ──────────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> int:
    base_url    = args.url.rstrip("/")
    ingest_url  = f"{base_url}/api/ingest"
    n_tracks    = args.tracks
    rate_hz     = args.rate          # updates/second across all workers
    duration_s  = args.duration
    n_workers   = min(args.workers, n_tracks)
    warmup_s    = min(10, duration_s // 4)
    api_key     = args.api_key

    track_ids = [f"ST-{uuid.uuid4().hex[:8].upper()}" for _ in range(n_tracks)]

    print(f"\n{'='*60}")
    print(f"  NIZAM Stress Test")
    print(f"  URL:      {base_url}")
    print(f"  Tracks:   {n_tracks}   Rate: {rate_hz} Hz   Duration: {duration_s}s")
    print(f"  Workers:  {n_workers}   Warmup: {warmup_s}s")
    print(f"{'='*60}")

    # Verify server is reachable
    m = _get_metrics(base_url, api_key)
    if m is None:
        print(f"\n  [ERROR] Cannot reach {base_url}/api/metrics — is the server running?")
        return 1
    print(f"\n  [OK] Server reachable — {m.get('state', {}).get('tracks', 0)} tracks active")

    # Split track IDs among workers
    chunks: List[List[str]] = [[] for _ in range(n_workers)]
    for i, tid in enumerate(track_ids):
        chunks[i % n_workers].append(tid)

    per_worker_rate = max(0.1, rate_hz / n_workers)

    # ── Inject ───────────────────────────────────────────────────────────────
    print(f"\n  Injecting {n_tracks} tracks for {duration_s}s ...")
    t_start = time.monotonic()
    total_ok  = 0
    total_err = 0

    with ThreadPoolExecutor(max_workers=n_workers, thread_name_prefix="stress") as pool:
        futures = [
            pool.submit(_worker, chunk, ingest_url, api_key,
                        per_worker_rate, duration_s)
            for chunk in chunks
        ]
        # Poll metrics every 5s during the run
        poll_deadline = t_start + duration_s
        next_poll = t_start + warmup_s
        samples: List[Dict[str, Any]] = []

        while time.monotonic() < poll_deadline:
            now = time.monotonic()
            if now >= next_poll:
                snap = _get_metrics(base_url, api_key)
                if snap:
                    tac = snap.get("tactical", {})
                    samples.append({
                        "t":    round(now - t_start, 1),
                        "p50":  tac.get("p50_ms", 0),
                        "p95":  tac.get("p95_ms", 0),
                        "p99":  tac.get("p99_ms", 0),
                        "ran":  tac.get("ran", 0),
                    })
                    print(
                        f"  t={samples[-1]['t']:5.1f}s  "
                        f"p50={samples[-1]['p50']:6.1f}ms  "
                        f"p95={samples[-1]['p95']:6.1f}ms  "
                        f"p99={samples[-1]['p99']:6.1f}ms  "
                        f"tactical_ran={samples[-1]['ran']}"
                    )
                next_poll = now + 5.0
            time.sleep(0.2)

        for f in as_completed(futures):
            c = f.result()
            total_ok  += c["ok"]
            total_err += c["err"]

    elapsed = time.monotonic() - t_start

    # ── Final metrics snapshot ────────────────────────────────────────────────
    final = _get_metrics(base_url, api_key)
    tac_final = (final or {}).get("tactical", {})
    p50  = tac_final.get("p50_ms", 0)
    p95  = tac_final.get("p95_ms", 0)
    p99  = tac_final.get("p99_ms", 0)
    runs = tac_final.get("ran", 0)

    # ── Report ────────────────────────────────────────────────────────────────
    throughput = total_ok / elapsed if elapsed > 0 else 0

    print(f"\n{'='*60}")
    print(f"  Results")
    print(f"{'='*60}")
    print(f"  Duration:   {elapsed:.1f}s")
    print(f"  Sent OK:    {total_ok}   Errors: {total_err}")
    print(f"  Throughput: {throughput:.1f} req/s")
    print(f"  Tactical runs: {runs}")
    print()
    print(f"  Latency (from /api/metrics):")
    p50c  = "" if p50  <= 500  else " ⚠" if p50  <= 1000 else " ✗"
    p95c  = "" if p95  <= DEFAULT_P95_WARN_MS else " ⚠"
    p99c  = "" if p99  <= DEFAULT_P99_FAIL_MS else " ✗"
    print(f"    p50 = {p50:7.1f} ms{p50c}")
    print(f"    p95 = {p95:7.1f} ms{p95c}  (warn >{DEFAULT_P95_WARN_MS}ms)")
    print(f"    p99 = {p99:7.1f} ms{p99c}  (fail >{DEFAULT_P99_FAIL_MS}ms)")

    if tac_final.get("module_ms"):
        print(f"\n  Per-module breakdown (last run):")
        for k, v in sorted(tac_final["module_ms"].items(), key=lambda x: -x[1]):
            print(f"    {k:<20} {v:.1f} ms")

    print(f"{'='*60}\n")

    # ── Pass/fail ─────────────────────────────────────────────────────────────
    if args.no_assert:
        return 0

    if p99 > DEFAULT_P99_FAIL_MS:
        print(f"  [FAIL] p99 {p99:.0f}ms > {DEFAULT_P99_FAIL_MS}ms threshold")
        return 1
    if p95 > DEFAULT_P95_WARN_MS:
        print(f"  [WARN] p95 {p95:.0f}ms > {DEFAULT_P95_WARN_MS}ms (not failing)")
    if total_err > total_ok * 0.05:
        print(f"  [FAIL] Error rate {100*total_err/(total_ok+total_err):.1f}% > 5%")
        return 1

    print("  [PASS] All thresholds met.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="NIZAM stress / load test")
    ap.add_argument("--url",       default="http://localhost:8100",
                    help="COP server base URL")
    ap.add_argument("--api_key",   default="", help="X-API-Key header")
    ap.add_argument("--tracks",    type=int,   default=50,
                    help="Number of synthetic tracks to inject (default 50)")
    ap.add_argument("--rate",      type=float, default=10.0,
                    help="Total ingest rate in events/sec (default 10)")
    ap.add_argument("--duration",  type=int,   default=30,
                    help="Test duration in seconds (default 30)")
    ap.add_argument("--workers",   type=int,   default=4,
                    help="Parallel sender threads (default 4)")
    ap.add_argument("--no_assert", action="store_true",
                    help="Print results but never return exit code 1")
    args = ap.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
