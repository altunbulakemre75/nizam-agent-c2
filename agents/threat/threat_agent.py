import argparse
import json
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
        "source": {
            "agent_id": source_agent,
            "instance_id": instance_id,
            "host": host
        },
        "correlation_id": correlation_id,
        "payload": payload
    }

def score_and_assess(range_m: float, radial_velocity_mps: float):
    reasons = []
    score = 0

    closing_speed = max(0.0, -radial_velocity_mps)
    if closing_speed > 5.0:
        score += 20
        reasons.append(f"Approaching target (closing_speed={closing_speed:.1f} m/s)")

    tti_s: Optional[float] = None
    if closing_speed > 0:
        tti_s = range_m / closing_speed
        if tti_s < 60.0:
            score += 40
            reasons.append(f"TTI < 60s (tti={tti_s:.1f}s)")

    if range_m < 500.0:
        score += 30
        reasons.append(f"Close range (<500m): {range_m:.1f}m")

    if score >= 80:
        level = "HIGH"
    elif score >= 50:
        level = "MEDIUM"
    else:
        level = "LOW"

    return level, score, tti_s, reasons

def main():
    ap = argparse.ArgumentParser(description="Threat scoring agent (radar-based v0)")
    ap.add_argument("--instance_id", default="threat-01")
    ap.add_argument("--host", default="dev")
    args = ap.parse_args()

    source_agent = "threat-agent"

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        ev = json.loads(line)
        if ev.get("event_type") != "sensor.detection.radar":
            continue

        correlation_id = ev.get("correlation_id", "unknown")
        ts = ev.get("timestamp", utc_now_iso())

        detections = ev.get("payload", {}).get("detections", [])
        for det in detections:
            r = det.get("range_m")
            vr = det.get("radial_velocity_mps")
            if r is None or vr is None:
                continue

            level, score, tti_s, reasons = score_and_assess(r, vr)

            payload = {
                "global_track_id": det.get("local_track_id", "unknown"),
                "threat_level": level,
                "score": score,
                "tti_s": None if tti_s is None else round(tti_s, 1),
                "rules_fired": [],
                "reasons": reasons,
                "recommended_action": "ALERT" if level in ("HIGH", "MEDIUM") else "OBSERVE"
            }

            out = make_envelope(
                event_type="threat.assessment",
                source_agent=source_agent,
                instance_id=args.instance_id,
                host=args.host,
                correlation_id=correlation_id,
                payload=payload,
                ts=ts
            )

            print(json.dumps(out, ensure_ascii=False), flush=True)

if __name__ == "__main__":
    main()
