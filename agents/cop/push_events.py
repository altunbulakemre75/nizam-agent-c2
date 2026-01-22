import argparse
import json
import time
from datetime import datetime

import httpx

def parse_ts(ts: str) -> float:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts).timestamp()

def main():
    ap = argparse.ArgumentParser(description="Push JSONL events to COP /ingest endpoint.")
    ap.add_argument("--in", dest="infile", required=True, help="Input JSONL file")
    ap.add_argument("--url", default="http://127.0.0.1:8000/ingest", help="Ingest URL")
    ap.add_argument("--speed", type=float, default=0.0, help="0=max speed, 1=real-time, 10=10x faster")
    ap.add_argument("--max_events", type=int, default=0, help="0=all")
    args = ap.parse_args()

    lines = open(args.infile, "r", encoding="utf-8").read().splitlines()
    events = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        ev = json.loads(ln)
        t = ev.get("timestamp")
        events.append((parse_ts(t) if t else None, ev))

    # base time
    base_t = None
    for t, _ in events:
        if t is not None:
            base_t = t
            break

    start_wall = time.time()

    sent = 0
    with httpx.Client(timeout=10.0) as client:
        for t, ev in events:
            if args.max_events and sent >= args.max_events:
                break

            if args.speed > 0 and base_t is not None and t is not None:
                desired = (t - base_t) / args.speed
                now_elapsed = time.time() - start_wall
                sleep_s = desired - now_elapsed
                if sleep_s > 0:
                    time.sleep(sleep_s)

            r = client.post(args.url, json=ev)
            if r.status_code >= 300:
                raise RuntimeError(f"POST failed: {r.status_code} {r.text}")

            sent += 1

    print(f"OK: pushed {sent} events")

if __name__ == "__main__":
    main()
