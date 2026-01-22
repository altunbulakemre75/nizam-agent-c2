from __future__ import annotations

import threading
import time
from typing import Optional

import cv2
import requests
from fastapi import FastAPI
from shared.schemas import TaskRequest, AgentResult, AgentName

app = FastAPI(title="Camera Agent")

# -----------------------
# Runtime state
# -----------------------
_running = False
_thread: Optional[threading.Thread] = None

_last_status = {
    "running": False,
    "source": None,
    "last_motion": None,
    "frames": 0,
    "last_event_sent": None,
}

# Orchestrator endpoint
ORCH_URL = "http://127.0.0.1:8000/run"

# Motion tuning
MOTION_PIXEL_THRESHOLD = 5000          # increase if too sensitive
EVENT_COOLDOWN_SEC = 1.0               # send at most 1 event per second


@app.get("/health")
def health():
    return {"ok": True, "service": "camera_agent", **_last_status}


@app.post("/task")
def task(task: TaskRequest):
    # Backward-compatible endpoint (your earlier tests keep working)
    return AgentResult(
        ok=True,
        agent=AgentName.camera,
        action=task.action,
        data={"message": "camera task executed"},
        error="",
    ).model_dump()


def _send_motion_event(source: str, ts: float, frames: int, motion_pixels: int) -> None:
    """
    Send a lightweight motion event to the orchestrator.
    Fail-safe: never crash the loop if orchestrator is down.
    """
    global _last_status

    payload = {
        "action": "motion_detected",
        "payload": {
            "source": f"camera-{source}" if source.isdigit() else "camera-stream",
            "timestamp": ts,
            "frames": frames,
            "motion_pixels": motion_pixels,
        },
    }

    try:
        requests.post(ORCH_URL, json=payload, timeout=0.5)
        _last_status["last_event_sent"] = ts
    except Exception:
        # Orchestrator may be down; ignore to keep sensor loop alive
        pass


def _motion_loop(source: str):
    global _running, _last_status

    cap = None
    try:
        # source can be "0" / "1" (webcam index) OR a URL (rtsp/http)
        if source.isdigit():
            cap = cv2.VideoCapture(int(source))
        else:
            cap = cv2.VideoCapture(source)

        if not cap.isOpened():
            _last_status.update({"running": False, "last_motion": None})
            _running = False
            return

        prev_gray = None
        frames = 0
        last_motion_ts = None
        last_sent_ts = 0.0

        while _running:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue

            frames += 1
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (21, 21), 0)

            motion = False
            motion_pixels = 0

            if prev_gray is not None:
                diff = cv2.absdiff(prev_gray, gray)
                thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)[1]
                motion_pixels = int(thresh.sum() / 255)

                if motion_pixels > MOTION_PIXEL_THRESHOLD:
                    motion = True
                    last_motion_ts = time.time()

            prev_gray = gray

            _last_status.update(
                {
                    "running": True,
                    "source": source,
                    "last_motion": last_motion_ts,
                    "frames": frames,
                }
            )

            # If motion detected, send event with cooldown
            if motion and last_motion_ts is not None:
                if (last_motion_ts - last_sent_ts) >= EVENT_COOLDOWN_SEC:
                    _send_motion_event(source, last_motion_ts, frames, motion_pixels)
                    last_sent_ts = last_motion_ts

            time.sleep(0.02)

    finally:
        if cap is not None:
            cap.release()
        _last_status.update({"running": False})
        _running = False


@app.post("/start")
def start(source: str = "0"):
    global _running, _thread, _last_status

    if _running:
        return {"ok": True, "running": True, "source": _last_status.get("source")}

    _running = True
    _last_status.update({"running": True, "source": source, "last_motion": None, "frames": 0})

    _thread = threading.Thread(target=_motion_loop, args=(source,), daemon=True)
    _thread.start()

    return {"ok": True, "running": True, "source": source}


@app.post("/stop")
def stop():
    global _running
    if not _running:
        return {"ok": True, "running": False}
    _running = False
    return {"ok": True, "running": False}
