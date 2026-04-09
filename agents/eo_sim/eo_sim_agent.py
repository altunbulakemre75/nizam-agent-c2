"""
eo_sim_agent.py  —  NIZAM electro-optical / camera sensor simulator (Phase 2)

Simulates a fixed EO/camera sensor with:
  - Configurable FOV (default: ±60° half-angle, wide to cover all tracks)
  - High angular accuracy: bearing noise ≈ 0.3° (vs RF 6°)
  - Probabilistic visual classification (drone / uav / bird / balloon)
  - Range hint (±25% visual estimate)
  - Complementary to RADAR and RF in the fusion pipeline

Input:  sensor.detection.radar events  (JSONL, stdin)
          — EO gates off radar scans to know where to look
Output: original radar lines (pass-through) + sensor.detection.eo  (JSONL, stdout)
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from typing import Any, Dict, List

from shared.utils import utc_now_iso, make_envelope


def _ang_diff(a: float, b: float) -> float:
    """Signed-magnitude angular difference (degrees)."""
    return abs(((a - b) + 180) % 360 - 180)


# ---------------------------------------------------------------------------
# Visual classification heuristic
# ---------------------------------------------------------------------------

def classify_visual(range_m: float, vr_mps: float, rng: random.Random) -> Dict[str, Any]:
    """
    Rough visual classification from observed kinematics.
    In a real system this comes from a CNN on camera frames.
    """
    speed = abs(vr_mps)

    if speed > 18.0:
        label, base = "drone", 0.85
    elif speed > 8.0:
        label, base = ("drone" if range_m < 400 else "uav"), 0.72
    else:
        # Ambiguous slow target
        label, base = rng.choices(
            ["drone", "bird", "balloon"],
            weights=[0.55, 0.30, 0.15],
        )[0], 0.55

    conf = float(rng.gauss(base, 0.06))
    conf = max(0.30, min(0.97, conf))
    return {"label": label, "conf": round(conf, 3)}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="NIZAM EO/camera sensor simulator")
    ap.add_argument("--instance_id",       default="eo-01")
    ap.add_argument("--host",              default="dev")
    ap.add_argument("--fov_half_deg",      type=float, default=60.0,
                    help="Half-angle FOV (degrees). Default 60° covers most scenarios.")
    ap.add_argument("--bore_az_deg",       type=float, default=0.0,
                    help="Camera bore-sight azimuth (degrees, 0 = North).")
    ap.add_argument("--prob_detect",       type=float, default=0.65,
                    help="Per-detection probability of EO confirming a radar target.")
    ap.add_argument("--bearing_noise_deg", type=float, default=0.3,
                    help="1-sigma bearing noise (degrees). EO is much tighter than RF.")
    args = ap.parse_args()

    source_agent = "eo-sim-agent"
    rng = random.Random()

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue

        # Always pass through the original line
        print(raw, flush=True)

        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue

        if ev.get("event_type") != "sensor.detection.radar":
            continue

        payload = ev.get("payload", {})
        dets: List[Dict] = payload.get("detections", [])
        if not dets:
            continue

        correlation_id = ev.get("correlation_id", "unknown")
        ts = ev.get("timestamp", utc_now_iso())

        eo_dets: List[Dict] = []
        for det in dets:
            az = det.get("az_deg")
            r  = det.get("range_m")
            vr = det.get("radial_velocity_mps", 0.0)
            if az is None or r is None:
                continue

            # FOV gate
            if _ang_diff(float(az), args.bore_az_deg) > args.fov_half_deg:
                continue

            # Probabilistic detection
            if rng.random() > args.prob_detect:
                continue

            # EO bearing (very accurate)
            bearing = (float(az) + rng.gauss(0, args.bearing_noise_deg)) % 360

            # Visual classification
            cls_hint = classify_visual(float(r), float(vr), rng)

            # Confidence: high at short range, degrades gently
            conf = float(rng.gauss(0.88 - float(r) / 6000.0, 0.05))
            conf = max(0.35, min(0.97, conf))

            eo_dets.append({
                "bearing_deg": round(bearing, 2),
                "conf": round(conf, 3),
                "classification_hint": cls_hint,
                "range_hint_m": round(float(r) * rng.uniform(0.78, 1.22), 1),
                "sensor_id": args.instance_id,
            })

        if eo_dets:
            eo_ev = make_envelope(
                event_type="sensor.detection.eo",
                source_agent=source_agent,
                instance_id=args.instance_id,
                host=args.host,
                correlation_id=correlation_id,
                payload={
                    "sensor": {
                        "sensor_id": args.instance_id,
                        "sensor_type": "EO",
                        "fov_half_deg": args.fov_half_deg,
                        "bore_az_deg": args.bore_az_deg,
                    },
                    "detections": eo_dets,
                },
                ts=ts,
            )
            print(json.dumps(eo_ev, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
