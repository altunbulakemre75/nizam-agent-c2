import argparse
import json
import math
import sys
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def wrap_deg(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0

def ang_diff_deg(a: float, b: float) -> float:
    return abs(wrap_deg(a - b))

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

class Track:
    def __init__(self, track_id: str):
        self.track_id = track_id
        self.last_ts: str = ""
        self.range_m: Optional[float] = None
        self.az_deg: Optional[float] = None
        self.el_deg: Optional[float] = None
        self.radial_velocity_mps: Optional[float] = None
        self.supporting_sensors: Dict[str, int] = {}
        self.evidence: List[str] = []

    def touch_sensor(self, s: str, note: str):
        self.supporting_sensors[s] = self.supporting_sensors.get(s, 0) + 1
        if note:
            self.evidence.append(note)

    def num_unique_sensors(self) -> int:
        return len(self.supporting_sensors.keys())

def main():
    ap = argparse.ArgumentParser(description="Fusion agent v0: 2+ sensor confirmation based on simple gating.")
    ap.add_argument("--instance_id", default="fuser-01")
    ap.add_argument("--host", default="dev")
    ap.add_argument("--bearing_gate_deg", type=float, default=12.0)
    ap.add_argument("--range_gate_m", type=float, default=250.0)
    ap.add_argument("--track_ttl_s", type=float, default=5.0, help="How long to keep tracks without updates (sim-time based on timestamps not enforced in v0)")
    args = ap.parse_args()

    source_agent = "fuser-agent"

    # v0 cache: key is "bucketed" by range and bearing to create stable-ish IDs
    tracks: Dict[str, Track] = {}

    def make_track_id(range_m: float, az_deg: float) -> str:
        # bucketize to reduce jitter
        r_bin = int(range_m // 100)   # 100m bins
        a_bin = int((az_deg + 180) // 10)  # 10deg bins
        return f"T-R{r_bin:03d}-A{a_bin:03d}"

    def find_best_track_for_rf(bearing_deg: float) -> Optional[Track]:
        best: Tuple[float, Optional[Track]] = (1e9, None)
        for tr in tracks.values():
            if tr.az_deg is None:
                continue
            d = ang_diff_deg(tr.az_deg, bearing_deg)
            if d < best[0]:
                best = (d, tr)
        if best[1] is None:
            return None
        if best[0] <= args.bearing_gate_deg:
            return best[1]
        return None

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        ev = json.loads(line)
        et = ev.get("event_type")
        correlation_id = ev.get("correlation_id", "unknown")
        ts = ev.get("timestamp", utc_now_iso())

        # 1) RADAR updates create/refresh tracks
        if et == "sensor.detection.radar":
            dets = ev.get("payload", {}).get("detections", [])
            for det in dets:
                r = det.get("range_m")
                az = det.get("az_deg")
                if r is None or az is None:
                    continue

                track_id = make_track_id(float(r), float(az))
                tr = tracks.get(track_id)
                if tr is None:
                    tr = Track(track_id)
                    tracks[track_id] = tr

                tr.last_ts = ts
                tr.range_m = float(r)
                tr.az_deg = float(az)
                tr.el_deg = float(det.get("el_deg", 0.0))
                tr.radial_velocity_mps = float(det.get("radial_velocity_mps", 0.0))
                tr.touch_sensor("RADAR", f"RADAR update: r={tr.range_m:.1f}m az={tr.az_deg:.1f}deg vr={tr.radial_velocity_mps:.1f}m/s")

            continue

        # 2) RF evidence attaches to nearest radar track (bearing gate)
        if et == "sensor.detection.rf":
            rf_dets = ev.get("payload", {}).get("detections", [])
            if not rf_dets:
                continue
            bearing = rf_dets[0].get("bearing_deg")
            conf = rf_dets[0].get("conf", None)
            if bearing is None:
                continue

            tr = find_best_track_for_rf(float(bearing))
            if tr is None:
                continue

            tr.touch_sensor("RF", f"RF confirm: bearing={float(bearing):.1f}deg conf={conf}")

            # Emit track.update whenever RF attaches
            track_payload = {
                "global_track_id": tr.track_id,
                "status": "CONFIRMED" if tr.num_unique_sensors() >= 2 else "TENTATIVE",
                "kinematics": {
                    "range_m": tr.range_m,
                    "az_deg": tr.az_deg,
                    "el_deg": tr.el_deg,
                    "radial_velocity_mps": tr.radial_velocity_mps
                },
                "classification": {"label": "drone", "conf": 0.7},  # v0 heuristic; later from camera
                "supporting_sensors": list(tr.supporting_sensors.keys()),
                "evidence": tr.evidence[-6:],  # last few
                "last_update_sources": ["RF"]
            }

            out_track = make_envelope(
                event_type="track.update",
                source_agent=source_agent,
                instance_id=args.instance_id,
                host=args.host,
                correlation_id=correlation_id,
                payload=track_payload,
                ts=ts
            )
            print(json.dumps(out_track, ensure_ascii=False), flush=True)

            # If 2+ sensors confirmed -> emit boosted threat assessment
            if tr.num_unique_sensors() >= 2:
                # Very simple: base score from radar closing + range, then +20 boost
                r = float(tr.range_m or 0.0)
                vr = float(tr.radial_velocity_mps or 0.0)
                closing = max(0.0, -vr)
                tti = (r / closing) if closing > 0 else None

                base = 0
                reasons = []
                if closing > 5:
                    base += 20
                    reasons.append(f"Approaching (closing_speed={closing:.1f} m/s)")
                if r < 500:
                    base += 30
                    reasons.append(f"Range < 500m ({r:.1f}m)")
                if tti is not None and tti < 60:
                    base += 40
                    reasons.append(f"TTI < 60s (tti={tti:.1f}s)")

                score = min(100, base + 20)  # +20 multi-sensor boost
                # promote level by score
                if score >= 80:
                    level = "HIGH"
                elif score >= 50:
                    level = "MEDIUM"
                else:
                    level = "LOW"

                reasons.append("Multi-sensor confirmed (RADAR+RF) => threat boost")

                threat_payload = {
                    "global_track_id": tr.track_id,
                    "threat_level": level,
                    "score": score,
                    "tti_s": None if tti is None else round(tti, 1),
                    "rules_fired": ["MULTI_SENSOR_CONFIRMED_2PLUS"],
                    "reasons": reasons,
                    "recommended_action": "ALERT" if level in ("HIGH","MEDIUM") else "OBSERVE"
                }

                out_threat = make_envelope(
                    event_type="threat.assessment",
                    source_agent=source_agent,
                    instance_id=args.instance_id,
                    host=args.host,
                    correlation_id=correlation_id,
                    payload=threat_payload,
                    ts=ts
                )
                print(json.dumps(out_threat, ensure_ascii=False), flush=True)

            continue

        # ignore other event types

if __name__ == "__main__":
    main()
