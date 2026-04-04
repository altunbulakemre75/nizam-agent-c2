"""
replay/player.py  —  COP Recording Playback Engine

Loads a recorded .jsonl file and provides:
  - Frame-by-frame access by elapsed time
  - Variable-speed playback state machine
  - Seek to any point in the recording
  - Recording metadata and listing

Playback state machine:
  IDLE → LOADED → PLAYING ⇄ PAUSED → IDLE
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RECORDINGS_DIR = Path(__file__).parent.parent / "recordings"

# ── Recording index ───────────────────────────────────────────────────────────

def list_recordings() -> List[Dict[str, Any]]:
    """List all available recordings with metadata."""
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    recordings = []
    for f in sorted(RECORDINGS_DIR.glob("*.jsonl"), reverse=True):
        try:
            meta = _read_meta(f)
            if meta:
                meta["filename"] = f.name
                meta["size_kb"] = round(f.stat().st_size / 1024, 1)
                recordings.append(meta)
        except Exception:
            continue
    return recordings


def _read_meta(path: Path) -> Optional[Dict[str, Any]]:
    """Read metadata header and footer from a recording file."""
    with open(path, "r", encoding="utf-8") as fh:
        first_line = fh.readline().strip()
        if not first_line:
            return None
        meta = json.loads(first_line)
        if not meta.get("meta"):
            return None

        # Try to read the last line for footer (duration, frame count)
        last_line = first_line
        for line in fh:
            line = line.strip()
            if line:
                last_line = line
        try:
            last_obj = json.loads(last_line)
            if last_obj.get("footer"):
                meta["total_frames"] = last_obj.get("total_frames", 0)
                meta["duration_s"] = last_obj.get("duration_s", 0)
            elif "frame" in last_obj:
                # No footer (server killed) — derive from last frame
                meta["total_frames"] = last_obj.get("frame", 0)
                meta["duration_s"] = last_obj.get("elapsed_s", 0)
        except Exception:
            pass

    return meta


# ── Player State Machine ─────────────────────────────────────────────────────

class Player:
    """
    In-memory playback engine for a single recording.

    Usage:
        player = Player()
        player.load("20260404_123456_coordinated_attack.jsonl")
        player.play(speed=1.0)
        frame = player.get_current_frame()  # returns snapshot at current time
        player.seek(30.0)  # jump to 30s
        player.pause()
        player.stop()
    """

    def __init__(self):
        self._state: str = "IDLE"  # IDLE, LOADED, PLAYING, PAUSED
        self._frames: List[Dict[str, Any]] = []
        self._meta: Dict[str, Any] = {}
        self._duration_s: float = 0.0
        self._filename: str = ""

        # Playback clock
        self._speed: float = 1.0
        self._play_start_wall: float = 0.0   # wall clock when play() was called
        self._play_start_elapsed: float = 0.0  # elapsed_s at play() time
        self._current_elapsed: float = 0.0     # current position in recording

        # Cache
        self._last_frame_idx: int = 0

    @property
    def state(self) -> str:
        return self._state

    @property
    def duration(self) -> float:
        return self._duration_s

    @property
    def elapsed(self) -> float:
        if self._state == "PLAYING":
            wall_delta = time.time() - self._play_start_wall
            self._current_elapsed = self._play_start_elapsed + wall_delta * self._speed
            # Clamp to duration
            if self._current_elapsed >= self._duration_s:
                self._current_elapsed = self._duration_s
                self._state = "PAUSED"
        return self._current_elapsed

    def load(self, filename: str) -> Dict[str, Any]:
        """Load a recording file. Returns metadata."""
        path = RECORDINGS_DIR / filename
        if not path.exists():
            raise FileNotFoundError(f"Recording not found: {filename}")

        self._frames = []
        self._meta = {}

        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if obj.get("meta"):
                    self._meta = obj
                elif obj.get("footer"):
                    self._duration_s = obj.get("duration_s", 0.0)
                elif "frame" in obj:
                    self._frames.append(obj)

        if not self._frames:
            raise ValueError(f"No frames found in recording: {filename}")

        # Derive duration from last frame if footer missing
        if self._duration_s == 0.0 and self._frames:
            self._duration_s = self._frames[-1].get("elapsed_s", 0.0)

        self._filename = filename
        self._current_elapsed = 0.0
        self._last_frame_idx = 0
        self._state = "LOADED"

        return self.get_info()

    def play(self, speed: float = 1.0) -> None:
        """Start or resume playback at given speed."""
        if self._state not in ("LOADED", "PAUSED"):
            return
        self._speed = max(0.1, min(speed, 20.0))
        self._play_start_wall = time.time()
        self._play_start_elapsed = self._current_elapsed
        self._state = "PLAYING"

    def pause(self) -> None:
        """Pause playback."""
        if self._state != "PLAYING":
            return
        # Freeze current position
        self._current_elapsed = self.elapsed
        self._state = "PAUSED"

    def stop(self) -> None:
        """Stop playback, unload recording."""
        self._frames = []
        self._meta = {}
        self._duration_s = 0.0
        self._current_elapsed = 0.0
        self._last_frame_idx = 0
        self._filename = ""
        self._state = "IDLE"

    def seek(self, elapsed_s: float) -> None:
        """Seek to a specific time position."""
        if self._state == "IDLE":
            return
        self._current_elapsed = max(0.0, min(elapsed_s, self._duration_s))
        self._last_frame_idx = 0  # reset search cache
        if self._state == "PLAYING":
            # Re-anchor the playback clock
            self._play_start_wall = time.time()
            self._play_start_elapsed = self._current_elapsed

    def set_speed(self, speed: float) -> None:
        """Change playback speed without interrupting."""
        speed = max(0.1, min(speed, 20.0))
        if self._state == "PLAYING":
            # Re-anchor
            self._current_elapsed = self.elapsed
            self._play_start_wall = time.time()
            self._play_start_elapsed = self._current_elapsed
        self._speed = speed

    def get_current_frame(self) -> Optional[Dict[str, Any]]:
        """Get the frame closest to current elapsed time."""
        if not self._frames:
            return None
        t = self.elapsed
        return self._frame_at(t)

    def get_frame_at(self, elapsed_s: float) -> Optional[Dict[str, Any]]:
        """Get frame at a specific elapsed time."""
        if not self._frames:
            return None
        return self._frame_at(elapsed_s)

    def _frame_at(self, t: float) -> Dict[str, Any]:
        """Binary search for the closest frame <= t."""
        frames = self._frames
        lo, hi = 0, len(frames) - 1

        # Quick check: beyond the end
        if t >= frames[-1]["elapsed_s"]:
            return frames[-1]
        if t <= 0:
            return frames[0]

        # Binary search
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if frames[mid]["elapsed_s"] <= t:
                lo = mid
            else:
                hi = mid - 1

        return frames[lo]

    def get_info(self) -> Dict[str, Any]:
        """Get recording metadata and playback status."""
        return {
            "state": self._state,
            "filename": self._filename,
            "scenario": self._meta.get("scenario", ""),
            "duration_s": round(self._duration_s, 2),
            "total_frames": len(self._frames),
            "current_elapsed_s": round(self.elapsed, 2),
            "speed": self._speed,
            "start_time_iso": self._meta.get("start_time_iso", ""),
        }


# ── Singleton player instance ────────────────────────────────────────────────

_player = Player()


def get_player() -> Player:
    return _player
