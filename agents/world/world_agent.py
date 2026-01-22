import argparse
import json
import math
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def wrap_deg(a: float) -> float:
    # wrap to [-180, 180)
    a = (a + 180.0) % 360.0 - 180.0
    return a

@dataclass
class Entity:
    entity_id: str
    label: str
    range_m: float
    az_deg: float
    speed_mps: float
    heading_deg: float  # in polar frame: 0=outward along +range, 180=inward to origin
    alive: bool = True

    def step(self, dt: float):
        """
        Simple polar kinematics relative to origin:
        - radial component controls range change
        - tangential component controls azimuth change
        heading_deg convention:
          180 => moving inward (range decreasing)
          0   => moving outward (range increasing)
          90/-90 => mostly tangential
        """
        # Decompose velocity into radial/tangential components
        # radial = v * cos(phi) where phi is angle from outward radial direction
        phi = math.radians(self.heading_deg)
        v_rad = self.speed_mps * math.cos(phi)        # + outward, - inward
        v_tan = self.speed_mps * math.sin(phi)        # + increases az

        # Update range
        self.range_m = max(0.0, self.range_m + v_rad * dt)

        # Update azimuth: omega = v_tan / r  (rad/s), convert to deg/s
        if self.range_m > 1e-6:
            omega = (v_tan / self.range_m)            # rad/s
            self.az_deg = wrap_deg(self.az_deg + math.degrees(omega) * dt)

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

def default_entities() -> List[Entity]:
    # Drone: approaching the origin
    drone = Entity(
        entity_id="E-DRONE-01",
        label="drone",
        range_m=1200.0,
        az_deg=15.0,
        speed_mps=25.0,
        heading_deg=180.0  # inward
    )
    # Vehicle: tangential-ish motion (side pass)
    veh = Entity(
        entity_id="E-VEH-01",
        label="vehicle",
        range_m=900.0,
        az_deg=-40.0,
        speed_mps=18.0,
        heading_deg=90.0   # mostly tangential (az changes)
    )
    return [drone, veh]

def main():
    ap = argparse.ArgumentParser(description="World agent producing ground-truth states in polar coordinates.")
    ap.add_argument("--correlation_id", default="demo-run-0002", help="Correlation ID for the run")
    ap.add_argument("--rate_hz", type=float, default=1.0, help="Update rate (Hz)")
    ap.add_argument("--duration_s", type=float, default=30.0, help="How long to run (seconds)")
    ap.add_argument("--origin_lat", type=float, default=None, help="Optional: origin latitude (for future geo mapping)")
    ap.add_argument("--origin_lon", type=float, default=None, help="Optional: origin longitude (for future geo mapping)")
    ap.add_argument("--stdout", action="store_true", help="Print events to stdout (JSONL)")
    args = ap.parse_args()

    dt = 1.0 / max(args.rate_hz, 1e-6)
    steps = int(args.duration_s * args.rate_hz)

    entities = default_entities()

    host = "dev"
    source_agent = "world-agent"
    instance_id = "world-01"

    # Use a stable start timestamp base for determinism of timestamp spacing
    t0_wall = time.time()
    t_prev = t0_wall

    for i in range(steps):
        now = time.time()
        # Keep dt stable based on rate_hz (not wall jitter)
        # This ensures deterministic kinematics even if wall timing drifts a bit.
        sim_t = i * dt

        # Step entities
        for e in entities:
            e.step(dt)

        payload = {
            "origin": {
                "lat": args.origin_lat,
                "lon": args.origin_lon
            },
            "sim_time_s": round(sim_t, 3),
            "entities": [
                {
                    "entity_id": e.entity_id,
                    "label": e.label,
                    "range_m": round(e.range_m, 3),
                    "az_deg": round(e.az_deg, 3),
                    "speed_mps": round(e.speed_mps, 3),
                    "heading_deg": round(e.heading_deg, 3)
                }
                for e in entities if e.alive
            ]
        }

        # Timestamp: use wall-clock ISO, but sim_time_s also included.
        ev = make_envelope(
            event_type="world.state",
            source_agent=source_agent,
            instance_id=instance_id,
            host=host,
            correlation_id=args.correlation_id,
            payload=payload,
            ts=utc_now_iso()
        )

        if args.stdout:
            print(json.dumps(ev, ensure_ascii=False), flush=True)

        # Sleep to maintain rate (best-effort)
        if args.rate_hz > 0:
            target = t0_wall + (i + 1) * dt
            sleep_s = target - time.time()
            if sleep_s > 0:
                time.sleep(sleep_s)

if __name__ == "__main__":
    main()
