import argparse
import json
import random
import sys
import uuid
from datetime import datetime, timezone
from typing import Optional

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def make_envelope(event_type, source_agent, instance_id, host, correlation_id, payload, ts=None):
    return {
        "schema_version": "1.1",
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "timestamp": ts or utc_now_iso(),
        "source": {"agent_id": source_agent, "instance_id": instance_id, "host": host},
        "correlation_id": correlation_id,
        "payload": payload
    }

def main():
    ap = argparse.ArgumentParser(description="RF sim agent: takes radar detections and emits RF detections (bearing).")
    ap.add_argument("--sensor_id", default="rf-01")
    ap.add_argument("--instance_id", default="rf-01")
    ap.add_argument("--host", default="dev")
    ap.add_argument("--prob_detect", type=float, default=0.7, help="Probability to emit an RF detection per radar scan")
    ap.add_argument("--bearing_noise_deg", type=float, default=6.0)
    ap.add_argument("--seed", type=int, default=2026)
    args = ap.parse_args()

    random.seed(args.seed)
    source_agent = "rf-sim-agent"

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        ev = json.loads(line)

        # We listen to radar scans and sometimes produce RF evidence
        if ev.get("event_type") != "sensor.detection.radar":
            continue

        correlation_id = ev.get("correlation_id", "unknown")
        ts = ev.get("timestamp", utc_now_iso())
        payload_in = ev.get("payload", {})
        dets = payload_in.get("detections", [])

        if not dets:
            continue

        # choose the strongest radar detection (highest conf)
        det = max(dets, key=lambda d: d.get("conf", 0.0))
        az = det.get("az_deg", None)
        if az is None:
            continue

        # emit with some probability
        if random.random() > args.prob_detect:
            continue

        bearing = float(az) + random.gauss(0.0, args.bearing_noise_deg)
        conf = max(0.05, min(0.95, 0.6 + random.gauss(0.0, 0.08)))

        payload_out = {
            "sensor": {"sensor_id": args.sensor_id, "sensor_type": "RF"},
            "window_ms": 1000,
            "detections": [
                {
                    "signal_type": "drone_control_suspected",
                    "band_hz": [2400000000, 2483500000],
                    "bearing_deg": round(bearing, 2),
                    "conf": round(conf, 3)
                }
            ]
        }

        out = make_envelope(
            event_type="sensor.detection.rf",
            source_agent=source_agent,
            instance_id=args.instance_id,
            host=args.host,
            correlation_id=correlation_id,
            payload=payload_out,
            ts=ts
        )

        print(json.dumps(out, ensure_ascii=False), flush=True)

if __name__ == "__main__":
    main()
