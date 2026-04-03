"""
replay.py  —  NIZAM replay launcher

Reads a saved JSONL log and replays it to the COP server.

Usage:
  python replay.py --in logs/run.jsonl
  python replay.py --in logs/run.jsonl --speed 5
  python replay.py --in logs/run.jsonl --speed 0   # max speed (no sleep)

Options:
  --in          JSONL file to replay (required)
  --cop_url     COP server base URL (default: http://127.0.0.1:8100)
  --speed       Replay speed multiplier: 1=real-time, 5=5x, 0=instant
  --loop        Loop the replay indefinitely
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def post_json(url: str, body: dict, timeout: float = 3.0) -> bool:
    data = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception as e:
        print(f"[replay] POST failed: {e}", file=sys.stderr)
        return False


def load_events(path: Path) -> list[tuple[float | None, dict]]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        ev = json.loads(line)
        events.append(ev)
    return events


def run_once(events: list[dict], ingest_url: str, speed: float) -> None:
    prev_ts: float | None = None

    for i, ev in enumerate(events):
        # Timing: use server_time in payload if available
        payload = ev.get("payload") or {}
        ts_str = payload.get("server_time")

        if speed > 0 and ts_str and prev_ts is not None:
            try:
                from datetime import datetime, timezone
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                gap = (ts - prev_ts) / speed
                if 0 < gap < 5.0:
                    time.sleep(gap)
                prev_ts = ts
            except Exception:
                pass
        elif speed > 0 and ts_str and prev_ts is None:
            try:
                from datetime import datetime, timezone
                prev_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
            except Exception:
                pass

        post_json(ingest_url, ev, timeout=3.0)

        if (i + 1) % 50 == 0:
            print(f"[replay] {i + 1}/{len(events)} events sent", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description="NIZAM replay launcher")
    ap.add_argument("--in",      dest="infile", required=True, help="JSONL replay file")
    ap.add_argument("--cop_url", default="http://127.0.0.1:8100")
    ap.add_argument("--speed",   type=float, default=1.0, help="1=real-time, 5=5x faster, 0=instant")
    ap.add_argument("--loop",    action="store_true", help="Loop replay indefinitely")
    args = ap.parse_args()

    path = Path(args.infile)
    if not path.exists():
        print(f"[replay] File not found: {path}", file=sys.stderr)
        sys.exit(1)

    ingest_url = args.cop_url.rstrip("/") + "/ingest"
    reset_url  = args.cop_url.rstrip("/") + "/api/reset"

    events = load_events(path)
    print(f"[replay] Loaded {len(events)} events from {path}", file=sys.stderr)
    print(f"[replay] Sending to: {ingest_url} @ {args.speed}x speed", file=sys.stderr)

    run_num = 0
    while True:
        run_num += 1
        # Reset COP state before each run
        post_json(reset_url, {}, timeout=3.0)
        time.sleep(0.3)

        print(f"[replay] Run #{run_num} starting...", file=sys.stderr)
        run_once(events, ingest_url, args.speed)
        print(f"[replay] Run #{run_num} complete.", file=sys.stderr)

        if not args.loop:
            break
        time.sleep(2.0)


if __name__ == "__main__":
    main()
