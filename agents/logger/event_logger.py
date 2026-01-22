import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def ensure_dir(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)

def iter_events_from_file(text: str):
    text = text.strip()
    if not text:
        return

    # JSON array
    if text.startswith("["):
        for obj in json.loads(text):
            yield obj
        return

    # Single JSON object
    if text.startswith("{"):
        yield json.loads(text)
        return

    raise ValueError("Unsupported JSON format in file")

def main():
    ap = argparse.ArgumentParser(description="Append events to a JSONL log file.")
    ap.add_argument("--out", required=True, help="Output JSONL file")
    ap.add_argument("--in", dest="infile", default=None, help="Input JSON file (object or array)")
    ap.add_argument("--enrich_ingest_time", action="store_true")
    args = ap.parse_args()

    out_path = Path(args.out)
    ensure_dir(out_path)

    with open(out_path, "a", encoding="utf-8") as out:

        # ✅ STDIN MODE (JSONL STREAM)
        if args.infile is None:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                ev = json.loads(line)
                if args.enrich_ingest_time:
                    ev["ingest_timestamp"] = utc_now_iso()
                out.write(json.dumps(ev, ensure_ascii=False) + "\n")
            return

        # ✅ FILE MODE (single JSON or array)
        text = Path(args.infile).read_text(encoding="utf-8-sig")
        for ev in iter_events_from_file(text):
            if args.enrich_ingest_time:
                ev["ingest_timestamp"] = utc_now_iso()
            out.write(json.dumps(ev, ensure_ascii=False) + "\n")

if __name__ == "__main__":
    main()
