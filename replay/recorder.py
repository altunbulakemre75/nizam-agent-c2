"""
replay/recorder.py  —  Full COP State Recorder

Records timestamped snapshots of the entire COP state during a live
scenario run.  Each snapshot captures:
  - All tracks, threats, zones, assets, tasks
  - AI predictions, anomalies, recommendations
  - Predictive breaches, uncertainty cones
  - Coordinated attacks, ROE advisories

Output: a single .jsonl file in recordings/ where each line is a frame:
  {"t": <unix>, "elapsed_s": <float>, "frame": <int>, "state": {...}}

The first line is always a metadata header:
  {"meta": true, "scenario": "...", "start_time": ..., "version": 1}
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

RECORDINGS_DIR = Path(__file__).parent.parent / "recordings"

# ── State ─────────────────────────────────────────────────────────────────────

_active: bool = False
_file = None
_start_time: float = 0.0
_frame_count: int = 0
_scenario_name: str = ""
_recording_path: Optional[Path] = None
_last_capture: float = 0.0
_min_interval: float = 0.5  # min seconds between captures (avoid flooding)


# ── API ───────────────────────────────────────────────────────────────────────

def start(scenario_name: str = "unnamed", min_interval: float = 0.5) -> str:
    """Begin recording. Returns the recording file path."""
    global _active, _file, _start_time, _frame_count
    global _scenario_name, _recording_path, _last_capture, _min_interval

    if _active:
        stop()

    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

    ts_str = time.strftime("%Y%m%d_%H%M%S")
    safe_name = scenario_name.replace(" ", "_").replace("/", "_").replace("\\", "_")
    filename = f"{ts_str}_{safe_name}.jsonl"
    _recording_path = RECORDINGS_DIR / filename

    _file = open(_recording_path, "w", encoding="utf-8")
    _start_time = time.time()
    _frame_count = 0
    _scenario_name = scenario_name
    _last_capture = 0.0
    _min_interval = min_interval
    _active = True

    # Write metadata header
    meta = {
        "meta": True,
        "version": 1,
        "scenario": scenario_name,
        "start_time": _start_time,
        "start_time_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_start_time)),
    }
    _file.write(json.dumps(meta, ensure_ascii=False) + "\n")
    _file.flush()

    return str(_recording_path)


MAX_TRACK_HISTORY = 10  # keep only last N history points per track in recording


def _trim_snapshot(state: Dict[str, Any]) -> Dict[str, Any]:
    """Trim large nested arrays to keep recording size manageable."""
    tracks = state.get("tracks")
    if isinstance(tracks, list):
        for t in tracks:
            hist = t.get("history")
            if isinstance(hist, list) and len(hist) > MAX_TRACK_HISTORY:
                t["history"] = hist[-MAX_TRACK_HISTORY:]
    return state


def capture_frame(snapshot_fn: Callable[[], Dict[str, Any]]) -> bool:
    """
    Capture a single frame. Takes a callable that returns the current
    snapshot dict (to avoid building it when not recording).
    Returns True if a frame was captured.
    """
    global _frame_count, _last_capture

    if not _active or _file is None:
        return False

    now = time.time()
    if now - _last_capture < _min_interval:
        return False

    _last_capture = now
    _frame_count += 1

    state = _trim_snapshot(snapshot_fn())

    frame = {
        "t": round(now, 3),
        "elapsed_s": round(now - _start_time, 3),
        "frame": _frame_count,
        "state": state,
    }

    _file.write(json.dumps(frame, ensure_ascii=False, default=str) + "\n")
    _file.flush()
    return True


def stop() -> Optional[Dict[str, Any]]:
    """Stop recording and close the file. Returns recording summary."""
    global _active, _file

    if not _active:
        return None

    duration = time.time() - _start_time

    # Write footer
    footer = {
        "footer": True,
        "total_frames": _frame_count,
        "duration_s": round(duration, 2),
        "end_time": time.time(),
    }
    if _file:
        _file.write(json.dumps(footer, ensure_ascii=False) + "\n")
        _file.flush()
        _file.close()
        _file = None

    _active = False

    return {
        "path": str(_recording_path),
        "scenario": _scenario_name,
        "frames": _frame_count,
        "duration_s": round(duration, 2),
    }


def is_active() -> bool:
    return _active


def get_status() -> Dict[str, Any]:
    if not _active:
        return {"recording": False}
    return {
        "recording": True,
        "scenario": _scenario_name,
        "frames": _frame_count,
        "elapsed_s": round(time.time() - _start_time, 2),
        "path": str(_recording_path),
    }
