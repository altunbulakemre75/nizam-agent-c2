"""
cop_publisher.py  —  NIZAM pipeline → COP bridge

Reads JSONL events from stdin, translates them into COP ingest payloads,
and POSTs them to the COP server's /ingest endpoint.

Supported input event types:
  track.update      → cop.track   (with range/az → lat/lon conversion)
  threat.assessment → cop.threat

Usage (pipeline tail):
  python agents/cop_publisher.py [--cop_url URL] [--origin_lat LAT] [--origin_lon LON]

Example full pipeline:
  python agents/world/world_agent.py \
    | python agents/radar_sim/radar_sim_agent.py \
    | tee >(python agents/rf_sim/rf_sim_agent.py) \
    | python agents/fuser/fuser_agent.py \
    | python agents/cop_publisher.py
"""

from __future__ import annotations

import argparse
import json
import math
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

# Allow `from shared.heartbeat import Heartbeat` when running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))
from shared.heartbeat import Heartbeat


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------

def polar_to_latlon(
    range_m: float,
    az_deg: float,
    origin_lat: float,
    origin_lon: float,
) -> tuple[float, float]:
    """
    Convert sensor-relative polar coords to WGS-84 lat/lon.

    Convention: az_deg is compass bearing (clockwise from North).
    Positive range = distance from sensor origin.
    """
    az_rad = math.radians(az_deg)
    # Displacement in meters
    dx = range_m * math.sin(az_rad)   # East
    dy = range_m * math.cos(az_rad)   # North

    # Metres per degree at origin latitude
    lat_deg_per_m = 1.0 / 111_320.0
    lon_deg_per_m = 1.0 / (111_320.0 * math.cos(math.radians(origin_lat)))

    lat = origin_lat + dy * lat_deg_per_m
    lon = origin_lon + dx * lon_deg_per_m
    return round(lat, 7), round(lon, 7)


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

import os as _os
_INGEST_API_KEY = _os.environ.get("INGEST_API_KEY", "")


def post_json(url: str, body: Dict[str, Any], timeout: float = 3.0) -> bool:
    """POST a JSON body; returns True on 2xx, False otherwise."""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if _INGEST_API_KEY:
        headers["X-API-Key"] = _INGEST_API_KEY
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        print(f"[cop_publisher] HTTP {e.code} → {url}: {e.read()[:200]}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[cop_publisher] POST failed → {url}: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Translators
# ---------------------------------------------------------------------------

def translate_track_update(payload: Dict[str, Any], origin_lat: float, origin_lon: float) -> Dict[str, Any]:
    """
    track.update payload → cop.track payload.

    Accepts either:
      - Direct lat/lon in payload (real sensor adapters)
      - kinematics.range_m + kinematics.az_deg (simulated agents, polar→WGS-84)
    Falls back to origin coords if neither is present.
    """
    gid = (
        payload.get("global_track_id")
        or payload.get("track_id")
        or payload.get("id")
        or "UNKNOWN"
    )

    kin = payload.get("kinematics") or {}
    range_m: Optional[float] = kin.get("range_m")
    az_deg: Optional[float] = kin.get("az_deg")

    # Real sensor adapters provide lat/lon directly — prefer these
    direct_lat = payload.get("lat")
    direct_lon = payload.get("lon")

    if direct_lat is not None and direct_lon is not None:
        lat, lon = float(direct_lat), float(direct_lon)
    elif range_m is not None and az_deg is not None:
        lat, lon = polar_to_latlon(float(range_m), float(az_deg), origin_lat, origin_lon)
    else:
        lat, lon = origin_lat, origin_lon

    # Convert polar history to lat/lon history for UI trail drawing
    history_latlon = []
    for h in payload.get("history", []):
        hr  = h.get("range_m")
        haz = h.get("az_deg")
        if hr is not None and haz is not None:
            hlat, hlon = polar_to_latlon(float(hr), float(haz), origin_lat, origin_lon)
            history_latlon.append({"lat": hlat, "lon": hlon, "ts": h.get("ts", "")})

    # Derive speed & heading from kinematics for LSTM trajectory predictor
    vr = float(kin.get("radial_velocity_mps") or 0.0)
    ak = float(kin.get("az_deg") or 0.0)
    speed = abs(vr)  # radial speed magnitude
    # heading: if approaching (vr<0) heading ≈ az + 180 (toward origin),
    #          if receding  (vr>0) heading ≈ az (away from origin)
    if vr < 0:
        heading = (ak + 180.0) % 360.0
    else:
        heading = ak % 360.0

    return {
        # Keys for COP server state store
        "global_track_id": gid,
        "id": gid,
        # Keys for Leaflet map rendering
        "lat": lat,
        "lon": lon,
        # Speed & heading (for LSTM trajectory predictor)
        "speed": round(speed, 2),
        "heading": round(heading, 2),
        # Passthrough fields
        "status": payload.get("status", "TENTATIVE"),
        "classification": payload.get("classification", {}),
        "supporting_sensors": payload.get("supporting_sensors", []),
        "kinematics": kin,
        "server_time": payload.get("server_time"),
        # Phase 2: intent + trail history (lat/lon)
        "intent":      payload.get("intent", "unknown"),
        "intent_conf": payload.get("intent_conf", 0.0),
        "history":     history_latlon,
        "threat_level": payload.get("threat_level"),
        "threat_score": payload.get("threat_score"),
    }


def translate_threat_assessment(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    threat.assessment payload → cop.threat payload.
    """
    gid = (
        payload.get("global_track_id")
        or payload.get("track_id")
        or payload.get("id")
        or "UNKNOWN"
    )

    return {
        "global_track_id": gid,
        "id": gid,
        "threat_level": payload.get("threat_level", "LOW"),
        "score": payload.get("score", 0),
        "tti_s": payload.get("tti_s"),
        "recommended_action": payload.get("recommended_action", "OBSERVE"),
        "reasons": payload.get("reasons", []),
        "rules_fired": payload.get("rules_fired", []),
        "server_time": payload.get("server_time"),
        "intent": payload.get("intent", "unknown"),
        "ml_probability": payload.get("ml_probability"),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Worker pool (decouples stdin reader from HTTP POST)
# ---------------------------------------------------------------------------
#
# Rationale: a naive synchronous POST loop blocks the stdin reader whenever
# the COP server is slow (heavy AI tick, WebSocket fan-out, etc.). Upstream
# pipeline processes then block writing to the pipe and the whole chain
# deadlocks. We fix this by putting a bounded in-memory queue between the
# stdin reader and a pool of HTTP worker threads, with a drop-oldest policy
# when the queue is full so the reader NEVER blocks on the network.

class _Metrics:
    """Thread-safe counters shared between reader and workers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self.skipped = 0

    def inc(self, field: str, n: int = 1) -> None:
        with self._lock:
            setattr(self, field, getattr(self, field) + n)

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return {
                "sent": self.sent,
                "failed": self.failed,
                "dropped": self.dropped,
                "skipped": self.skipped,
            }


_STOP_SENTINEL: Dict[str, Any] = {"__stop__": True}


def _worker_loop(
    q: "queue.Queue[Dict[str, Any]]",
    ingest_url: str,
    metrics: _Metrics,
    retry_delay: float,
) -> None:
    """Pull bodies off the queue and POST them until sentinel is received."""
    while True:
        body = q.get()
        try:
            if body.get("__stop__"):
                return
            ok = post_json(ingest_url, body, timeout=3.0)
            if ok:
                metrics.inc("sent")
            else:
                metrics.inc("failed")
                # Soft backoff so we don't hammer a downed server; the bounded
                # queue's drop-oldest policy handles the real pressure.
                time.sleep(retry_delay)
        finally:
            q.task_done()


def _enqueue_drop_oldest(
    q: "queue.Queue[Dict[str, Any]]",
    body: Dict[str, Any],
    metrics: _Metrics,
) -> None:
    """
    Non-blocking enqueue. If the queue is full, discard the oldest item so
    the newest sensor state always reaches the COP. In a live operational
    picture, newer data is strictly more valuable than stale data.
    """
    try:
        q.put_nowait(body)
    except queue.Full:
        try:
            q.get_nowait()
            q.task_done()
            metrics.inc("dropped")
        except queue.Empty:
            pass
        try:
            q.put_nowait(body)
        except queue.Full:
            metrics.inc("dropped")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="NIZAM pipeline → COP bridge: reads track/threat events from stdin, POSTs to COP /ingest"
    )
    ap.add_argument("--cop_url", default="http://127.0.0.1:8100", help="COP server base URL")
    ap.add_argument("--origin_lat", type=float, default=41.015, help="Sensor origin latitude (WGS-84)")
    ap.add_argument("--origin_lon", type=float, default=28.979, help="Sensor origin longitude (WGS-84)")
    ap.add_argument("--retry_delay", type=float, default=0.5, help="Seconds to wait after a failed POST")
    ap.add_argument("--passthrough", action="store_true", help="Echo each input line to stdout (for chaining)")
    ap.add_argument("--orchestrator_url", default="http://127.0.0.1:8200", help="Orchestrator base URL")
    ap.add_argument("--log_out", default=None, help="Save all COP ingest events to this JSONL file for replay")
    ap.add_argument("--workers", type=int, default=4,
                    help="Number of HTTP worker threads (parallel POSTs to /ingest)")
    ap.add_argument("--queue_size", type=int, default=1000,
                    help="Bounded outbound queue size (drops oldest on overflow)")
    args = ap.parse_args()

    ingest_url = args.cop_url.rstrip("/") + "/ingest"
    print(f"[cop_publisher] Starting. Ingesting to: {ingest_url}", file=sys.stderr)
    print(f"[cop_publisher] Origin: lat={args.origin_lat}, lon={args.origin_lon}", file=sys.stderr)
    print(f"[cop_publisher] Workers: {args.workers}  Queue size: {args.queue_size}", file=sys.stderr)

    log_file = None
    if args.log_out:
        import os
        os.makedirs(os.path.dirname(os.path.abspath(args.log_out)), exist_ok=True)
        log_file = open(args.log_out, "w", encoding="utf-8")
        print(f"[cop_publisher] Logging to: {args.log_out}", file=sys.stderr)

    # Register with orchestrator and start heartbeat
    hb = Heartbeat(
        name="cop-publisher",
        orchestrator_url=args.orchestrator_url,
        capabilities=["cop.track", "cop.threat"],
    )
    hb.start()

    # ── Worker pool setup ────────────────────────────────────────────────
    metrics = _Metrics()
    out_q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=args.queue_size)
    workers: list[threading.Thread] = []
    for i in range(max(1, args.workers)):
        t = threading.Thread(
            target=_worker_loop,
            args=(out_q, ingest_url, metrics, args.retry_delay),
            name=f"cop-pub-worker-{i}",
            daemon=True,
        )
        t.start()
        workers.append(t)

    # ── Reader loop (main thread) ────────────────────────────────────────
    # The reader NEVER blocks on network I/O. It only parses JSON, translates
    # to COP payload, and enqueues. This keeps the stdin pipe drained so
    # upstream pipeline stages never back up.
    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue

        if args.passthrough:
            print(raw_line, flush=True)

        try:
            ev = json.loads(raw_line)
        except json.JSONDecodeError as e:
            print(f"[cop_publisher] JSON parse error: {e} | line: {raw_line[:80]}", file=sys.stderr)
            metrics.inc("skipped")
            continue

        event_type: str = ev.get("event_type", "")
        payload: Dict[str, Any] = ev.get("payload") or {}

        if event_type == "track.update":
            cop_payload = translate_track_update(payload, args.origin_lat, args.origin_lon)
            body = {"event_type": "cop.track", "payload": cop_payload}

        elif event_type == "threat.assessment":
            cop_payload = translate_threat_assessment(payload)
            body = {"event_type": "cop.threat", "payload": cop_payload}

        else:
            # Silently ignore unrecognised event types (world.state, sensor.detection.*, etc.)
            continue

        if log_file:
            log_file.write(json.dumps(body, ensure_ascii=False) + "\n")
            log_file.flush()

        _enqueue_drop_oldest(out_q, body, metrics)

        snap = metrics.snapshot()
        total = snap["sent"] + snap["failed"]
        if total and total % 50 == 0:
            print(
                f"[cop_publisher] sent={snap['sent']} failed={snap['failed']} "
                f"dropped={snap['dropped']} skipped={snap['skipped']} "
                f"qsize={out_q.qsize()}",
                file=sys.stderr,
            )
            hb.report(**snap, qsize=out_q.qsize())

    # ── Drain + shutdown ─────────────────────────────────────────────────
    print("[cop_publisher] stdin closed, draining queue...", file=sys.stderr)
    out_q.join()
    for _ in workers:
        out_q.put(_STOP_SENTINEL)
    for t in workers:
        t.join(timeout=2.0)

    final = metrics.snapshot()
    print(
        f"[cop_publisher] final sent={final['sent']} failed={final['failed']} "
        f"dropped={final['dropped']} skipped={final['skipped']}",
        file=sys.stderr,
    )

    if log_file:
        log_file.close()


if __name__ == "__main__":
    main()
