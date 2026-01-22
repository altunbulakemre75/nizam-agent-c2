import argparse
import json
import math
import random
import sys
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def wrap_deg(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0

def make_envelope(event_type: str, source_agent: str, instance_id: str, host: str,
                  correlation_id: str, payload: dict, ts: Optional[str] = None) -> dict:
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

def main():
    ap = argparse.ArgumentParser(description="Radar simulation agent: world.state -> sensor.detection.radar")
    ap.add_argument("--sensor_id", default="radar-01")
    ap.add_argument("--instance_id", default="radar-01")
    ap.add_argument("--host", default="dev")

    # Noise model (std dev)
    ap.add_argument("--sigma_range_m", type=float, default=8.0)
    ap.add_argument("--sigma_az_deg", type=float, default=1.5)
    ap.add_argument("--sigma_el_deg", type=float, default=0.6)
    ap.add_argument("--sigma_vr_mps", type=float, default=1.5)

    # Detection behavior
    ap.add_argument("--dropout_prob", type=float, default=0.05)
    ap.add_argument("--false_positive_rate", type=float, default=0.15, help="Expected false detections per scan")
    ap.add_argument("--snr_base_db", type=float, default=20.0)
    ap.add_argument("--conf_base", type=float, default=0.8)

    # For demo: optional fixed elevation (simple)
    ap.add_argument("--fixed_el_deg", type=float, default=1.0)

    # Random seed for determinism (important for replay tests)
    ap.add_argument("--seed", type=int, default=1337)

    args = ap.parse_args()
    random.seed(args.seed)

    source_agent = "radar-sim-agent"

    # Read world.state events from stdin (JSONL)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        ev_in = json.loads(line)
        if ev_in.get("event_type") != "world.state":
            # ignore other event types
            continue

        correlation_id = ev_in.get("correlation_id", "unknown")
        ts = ev_in.get("timestamp", utc_now_iso())

        payload_in = ev_in.get("payload", {})
        entities = payload_in.get("entities", [])
        sim_time_s = payload_in.get("sim_time_s", None)

        scan_id = f"{args.sensor_id}:{sim_time_s}" if sim_time_s is not None else f"{args.sensor_id}:{uuid.uuid4()}"

        detections_out: List[Dict[str, Any]] = []

        # True detections from entities
        for ent in entities:
            # dropout (miss)
            if random.random() < args.dropout_prob:
                continue

            ent_id = ent.get("entity_id", "E-UNKNOWN")
            gt_r = float(ent.get("range_m", 0.0))
            gt_az = float(ent.get("az_deg", 0.0))
            gt_speed = float(ent.get("speed_mps", 0.0))
            gt_heading = float(ent.get("heading_deg", 180.0))

            # Approximate radial velocity: v_rad = v * cos(phi)
            # heading_deg: 180 => inward => cos(pi)=-1 => negative (approaching)
            phi = math.radians(gt_heading)
            gt_vr = gt_speed * math.cos(phi)

            # Add noise
            meas_r = max(0.0, gt_r + random.gauss(0.0, args.sigma_range_m))
            meas_az = wrap_deg(gt_az + random.gauss(0.0, args.sigma_az_deg))
            meas_el = args.fixed_el_deg + random.gauss(0.0, args.sigma_el_deg)
            meas_vr = gt_vr + random.gauss(0.0, args.sigma_vr_mps)

            # Simple confidence/SNR model: degrade with range
            snr = args.snr_base_db - (meas_r / 1000.0) * 6.0 + random.gauss(0.0, 1.0)
            conf = max(0.05, min(0.99, args.conf_base - (meas_r / 4000.0) + random.gauss(0.0, 0.03)))

            det = {
                "local_track_id": f"gt:{ent_id}",  # for debug (phase 2: keep this)
                "range_m": round(meas_r, 3),
                "az_deg": round(meas_az, 3),
                "el_deg": round(meas_el, 3),
                "radial_velocity_mps": round(meas_vr, 3),
                "snr_db": round(snr, 2),
                "conf": round(conf, 3),
                "covariance": {
                    "range_var": round(args.sigma_range_m ** 2, 3),
                    "az_var": round(args.sigma_az_deg ** 2, 3),
                    "el_var": round(args.sigma_el_deg ** 2, 3),
                    "vr_var": round(args.sigma_vr_mps ** 2, 3)
                }
            }
            detections_out.append(det)

        # False positives (Poisson-like using rate as expected count)
        # For simplicity: generate N where N is 0 or 1 or 2 around rate.
        # (Good enough for Phase 2 demo.)
        expected = max(0.0, args.false_positive_rate)
        n_fp = 0
        if expected > 0:
            # crude sampling: probability of 1 FP ~ expected (capped), 2 FP smaller
            p1 = min(0.8, expected)
            p2 = min(0.3, max(0.0, expected - 0.6))
            r = random.random()
            if r < p2:
                n_fp = 2
            elif r < p1:
                n_fp = 1

        for k in range(n_fp):
            meas_r = random.uniform(200.0, 2500.0)
            meas_az = random.uniform(-90.0, 90.0)
            meas_el = args.fixed_el_deg + random.gauss(0.0, args.sigma_el_deg)
            meas_vr = random.uniform(-35.0, 35.0)
            snr = args.snr_base_db - (meas_r / 1000.0) * 6.0 + random.gauss(0.0, 2.0)
            conf = max(0.05, min(0.7, 0.35 + random.gauss(0.0, 0.1)))

            detections_out.append({
                "local_track_id": f"fp:{uuid.uuid4().hex[:6]}",
                "range_m": round(meas_r, 3),
                "az_deg": round(meas_az, 3),
                "el_deg": round(meas_el, 3),
                "radial_velocity_mps": round(meas_vr, 3),
                "snr_db": round(snr, 2),
                "conf": round(conf, 3),
                "covariance": {
                    "range_var": round(args.sigma_range_m ** 2, 3),
                    "az_var": round(args.sigma_az_deg ** 2, 3),
                    "el_var": round(args.sigma_el_deg ** 2, 3),
                    "vr_var": round(args.sigma_vr_mps ** 2, 3)
                }
            })

        payload_out = {
            "sensor": {"sensor_id": args.sensor_id, "sensor_type": "RADAR"},
            "scan_id": scan_id,
            "sim_time_s": sim_time_s,
            "detections": detections_out
        }

        ev_out = make_envelope(
            event_type="sensor.detection.radar",
            source_agent=source_agent,
            instance_id=args.instance_id,
            host=args.host,
            correlation_id=correlation_id,
            payload=payload_out,
            ts=ts
        )

        print(json.dumps(ev_out, ensure_ascii=False), flush=True)

if __name__ == "__main__":
    main()
