"""
cop/engine/scenario_runner.py  —  In-process scenario playback

Loads a scenario JSON from `scenarios/` and replays it inside the COP
server process — no orchestrator, no subprocess, no agent registration.
The runner generates synthetic `cop.track` and `cop.threat` events,
mutates STATE under STATE_LOCK, and broadcasts to all WS clients. The
existing AI pipeline (fusion, anomaly, tactical, ML, ROE, auto-tasking)
runs on top untouched.

Why in-process:
  - Demo deployments (Railway, single-node) don't run the agent fleet.
  - Investors clicking "run" need a deterministic, single-click experience.
  - The runner is small enough that it doesn't justify a separate service.

Public API:
    start(scenario_name)   → schedule + return status
    stop()                 → stop the running scenario
    status()               → current runner state

Only one scenario can run at a time. start() returns an error if a
scenario is already in flight.
"""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

from cop.helpers import utc_now_iso
from cop.state import STATE, STATE_LOCK
from cop.ws_broadcast import broadcast, append_event_tail

# Full AI pipeline — same functions the /ingest handler calls. Without
# these the right-side panels (ML, ROE, coord-attack, anomalies, auto-
# generated tasks) stay empty because nothing triggered them.
from cop.engine.ai_pipeline import process_track as _process_track
from cop.engine.ai_pipeline import schedule_ai_tactical as _schedule_ai_tactical

SCENARIOS_DIR = Path(__file__).parent.parent.parent / "scenarios"

# Default origin (Istanbul Bosphorus area — matches existing scenario fixtures).
_DEFAULT_ORIGIN_LAT = 41.015
_DEFAULT_ORIGIN_LON = 28.979

# Earth radius for the small-angle polar→lat/lon projection. Accurate
# enough for scenarios with range < ~100 km from origin.
_R_EARTH = 6371000.0

# Singleton state — at most one scenario runs at a time.
_state: Dict[str, Any] = {
    "running":      False,
    "scenario":     None,
    "started_at":   None,
    "current_tick": 0,
    "total_ticks":  0,
    "duration_s":   0.0,
    "entity_count": 0,
}
_task:       Optional[asyncio.Task] = None
_stop_event: Optional[asyncio.Event] = None


def status() -> Dict[str, Any]:
    """Snapshot of the runner's current state. Safe to call any time."""
    return dict(_state)


def _polar_to_latlon(origin_lat: float, origin_lon: float,
                     range_m: float, az_deg: float) -> tuple[float, float]:
    """Convert polar offset (range, azimuth) to lat/lon. Small-angle approx."""
    az = math.radians(az_deg)
    d_lat = (range_m * math.cos(az)) / _R_EARTH * (180.0 / math.pi)
    d_lon = ((range_m * math.sin(az)) / _R_EARTH * (180.0 / math.pi)
             / max(math.cos(math.radians(origin_lat)), 1e-6))
    return origin_lat + d_lat, origin_lon + d_lon


def _intent_for_label(label: str, heading_deg: float) -> str:
    """Map (entity label, heading) → intent string used by the threat layer."""
    inward = abs(heading_deg - 180.0) < 60.0  # roughly inbound
    if label == "drone" and inward:
        return "attack"
    if label == "missile":
        return "attack"
    if label in ("helicopter", "fixed_wing"):
        return "reconnaissance"
    if label == "vehicle":
        return "loitering"
    return "unknown"


def _threat_level_for(intent: str, range_m: float) -> tuple[str, int]:
    """Heuristic threat level + score so the demo lights up the auto-tasking flow."""
    if intent == "attack" and range_m < 2000:
        return "HIGH", 85
    if intent == "attack":
        return "MEDIUM", 60
    if intent == "reconnaissance":
        return "MEDIUM", 50
    return "LOW", 25


async def _run(scenario: Dict[str, Any]) -> None:
    """Async loop that drives the scenario forward one tick at a time."""
    global _state

    duration_s = float(scenario.get("duration_s", 60))
    rate_hz    = float(scenario.get("rate_hz", 1.0))
    dt         = 1.0 / max(rate_hz, 0.1)
    total_ticks = max(1, int(duration_s * rate_hz))

    origin_lat = float(scenario.get("origin_lat", _DEFAULT_ORIGIN_LAT))
    origin_lon = float(scenario.get("origin_lon", _DEFAULT_ORIGIN_LON))

    # Mutable polar state per entity (we update range/az each tick).
    # init_* fields preserve the spawn position so the entity can loop back
    # to its starting point instead of bouncing — keeps threat intent stable
    # throughout the scenario (demo-friendly: no green flicker on outbound leg).
    entities: List[Dict[str, Any]] = []
    for e in scenario.get("entities", []):
        r0 = float(e["range_m"])
        a0 = float(e["az_deg"])
        h0 = float(e.get("heading_deg", 180.0))
        entities.append({
            "id":           e["entity_id"],
            "label":        e.get("label", "unknown"),
            "range_m":      r0,
            "az_deg":       a0,
            "speed_mps":    float(e.get("speed_mps", 20.0)),
            "heading_deg":  h0,
            # Frozen initial pose — used for loop-respawn on close-in.
            "init_range_m": r0,
            "init_az_deg":  a0,
            "init_heading": h0,
            # Intent is fixed at spawn time so a retreating entity stays
            # classified correctly rather than flipping to "unknown".
            "intent":       _intent_for_label(e.get("label", "unknown"), h0),
        })

    _state.update({
        "running":      True,
        "scenario":     scenario.get("name", "unknown"),
        "started_at":   utc_now_iso(),
        "current_tick": 0,
        "total_ticks":  total_ticks,
        "duration_s":   duration_s,
        "entity_count": len(entities),
    })

    # Close-in threshold: below this range the entity teleports back to its
    # spawn position and resumes its original heading. This creates a looping
    # "continuous approach" that keeps threat-level and icon color stable for
    # the entire scenario duration — no green flicker from the outbound leg.
    _MIN_RANGE_M = 120.0

    try:
        for tick in range(total_ticks):
            if _stop_event is not None and _stop_event.is_set():
                break

            now_iso = utc_now_iso()
            for ent in entities:
                # Polar kinematics — same convention as agents/world/world_agent.py.
                phi   = math.radians(ent["heading_deg"])
                v_rad = ent["speed_mps"] * math.cos(phi)   # +outward
                v_tan = ent["speed_mps"] * math.sin(phi)   # +increases azimuth
                ent["range_m"] = max(0.0, ent["range_m"] + v_rad * dt)
                if ent["range_m"] > 1e-6:
                    omega_deg = math.degrees(v_tan / ent["range_m"]) * dt
                    ent["az_deg"] = ((ent["az_deg"] + omega_deg + 540.0) % 360.0) - 180.0

                # Loop-respawn: when the entity closes within MIN_RANGE_M,
                # teleport it back to its initial spawn position and continue
                # with the original heading. Threat intent never resets.
                if ent["range_m"] <= _MIN_RANGE_M:
                    ent["range_m"]     = ent["init_range_m"]
                    ent["az_deg"]      = ent["init_az_deg"]
                    ent["heading_deg"] = ent["init_heading"]

                lat, lon = _polar_to_latlon(origin_lat, origin_lon,
                                            ent["range_m"], ent["az_deg"])
                intent = ent["intent"]  # fixed at spawn, never recalculated

                track_payload = {
                    "id":          ent["id"],
                    "lat":         round(lat, 6),
                    "lon":         round(lon, 6),
                    "speed":       round(ent["speed_mps"], 2),
                    "heading":     round(ent["heading_deg"], 1),
                    "altitude":    100.0,
                    "intent":      intent,
                    "classification": {"label": ent["label"], "confidence": 0.85},
                    "supporting_sensors": ["sim"],
                    "server_time": now_iso,
                }

                threat_level, score = _threat_level_for(intent, ent["range_m"])
                threat_payload = {
                    "id":           ent["id"],
                    "track_id":     ent["id"],
                    "threat_level": threat_level,
                    "score":        score,
                    "intent":       intent,
                    "tti_s":        round(ent["range_m"] / max(ent["speed_mps"], 1.0), 1),
                    "server_time":  now_iso,
                }

                async with STATE_LOCK:
                    STATE["tracks"][ent["id"]]  = track_payload
                    STATE["threats"][ent["id"]] = threat_payload
                    append_event_tail({"event_type": "cop.track",  "payload": track_payload})
                    append_event_tail({"event_type": "cop.threat", "payload": threat_payload})
                    # Fire the full per-track AI pipeline so the right-side
                    # panels light up: Kalman, trajectory, anomaly.
                    _process_track(ent["id"], track_payload["lat"],
                                   track_payload["lon"], intent)
                    # Auto-task generation — operator task queue comes from here.
                    # Imported lazily to break the import cycle (ingest.py
                    # already imports from cop.engine.scenario_runner's siblings).
                    from cop.routers.ingest import _auto_task
                    await _auto_task(ent["id"], threat_payload)

                await broadcast({"event_type": "cop.track",  "payload": track_payload})
                await broadcast({"event_type": "cop.threat", "payload": threat_payload})

            # Tactical analysis — swarm detect, coord attack, ML threat,
            # ROE, confidence. Rate-limited internally so calling it every
            # tick is fine; it'll run on its own cadence.
            _schedule_ai_tactical()

            _state["current_tick"] = tick + 1
            await asyncio.sleep(dt)

    finally:
        _state["running"] = False


def start(scenario_name: str) -> Dict[str, Any]:
    """Start playing a scenario by file stem (e.g. 'swarm_attack')."""
    global _task, _stop_event

    if _state["running"]:
        return {"ok": False, "error": "scenario already running"}

    safe = scenario_name.replace("/", "").replace("\\", "").replace("..", "")
    path = SCENARIOS_DIR / f"{safe}.json"
    if not path.exists():
        return {"ok": False, "error": f"scenario not found: {safe}"}

    try:
        scenario = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"failed to load scenario: {exc}"}

    scenario["name"] = safe
    # Mark running BEFORE we hand off to the event loop. Without this,
    # a second start() call between create_task() and _run()'s first
    # statement would see running=False and double-schedule. The _run()
    # body re-asserts the same flag once it actually starts.
    _state["running"]  = True
    _state["scenario"] = safe
    _stop_event = asyncio.Event()
    _task = asyncio.create_task(_run(scenario))
    return {"ok": True, "scenario": safe, "status": status()}


def stop() -> Dict[str, Any]:
    """Signal the running scenario to stop after its current tick."""
    if not _state["running"]:
        return {"ok": True, "already_stopped": True}
    if _stop_event is not None:
        _stop_event.set()
    return {"ok": True, "status": status()}
