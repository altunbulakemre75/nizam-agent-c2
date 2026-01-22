import argparse
import json
import time
from datetime import datetime
from pathlib import Path

def parse_ts(ts: str) -> float:
    # Accept ISO-8601 'Z' timestamps
    # Example: 2026-01-22T00:00:01.250Z
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts).timestamp()

def main():
    ap = argparse.ArgumentParser(description="Replay JSONL event logs with optional timing.")
    ap.add_argument("--in", dest="infile", required=True, help="Input JSONL file path")
    ap.add_argument("--speed", type=float, default=1.0, help="1.0=real-time, 10=10x faster, 0=max speed (no sleep)")
    ap.add_argument("--max_events", type=int, default=0, help="0=all")
    args = ap.parse_args()

    in_path = Path(args.infile)
    lines = in_path.read_text(encoding="utf-8").splitlines()

    events = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        ev = json.loads(line)
        ts = ev.get("timestamp")
        if not ts:
            # If missing timestamp, treat as same time
            t = None
        else:
            t = parse_ts(ts)
        events.append((t, ev))

    if not events:
        return

    # Establish base time from first event that has timestamp
    base_t = None
    for t, _ in events:
        if t is not None:
            base_t = t
            break

    start_wall = time.time()

    count = 0
    for t, ev in events:
        if args.max_events and count >= args.max_events:
            break

        if args.speed > 0 and base_t is not None and t is not None:
            # desired elapsed (scaled)
            desired = (t - base_t) / args.speed
            now_elapsed = time.time() - start_wall
            sleep_s = desired - now_elapsed
            if sleep_s > 0:
                time.sleep(sleep_s)

        # Emit event to stdout as JSON line
        print(json.dumps(ev, ensure_ascii=False), flush=True)
        count += 1

if __name__ == "__main__":
    main()
