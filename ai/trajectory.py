"""
ai/trajectory.py — LSTM trajectory predictor

Maintains a sliding window of (lat, lon, speed, heading) per track.
When the window is full, runs a trained LSTM to predict the next
PRED_STEPS positions and returns them as (lat, lon) waypoints.

Falls back gracefully when PyTorch is not installed or the model file
does not exist yet (run train_trajectory.py first).
"""
from __future__ import annotations

import logging
import math
import os
import time
from collections import defaultdict, deque
from typing import Dict, List, Optional

log = logging.getLogger("nizam.trajectory")

# ── Optional PyTorch ─────────────────────────────────────────────────────────
try:
    import numpy as np
    import torch
    import torch.nn as nn
    _TORCH_OK = True
except ImportError:
    _TORCH_OK = False
    log.warning("[trajectory] PyTorch/NumPy not installed — LSTM disabled")

# ── Constants ─────────────────────────────────────────────────────────────────
SEQ_LEN     = 8       # min observations before predicting (lowered for track-ID churn)
MODEL_SEQ   = 20      # model was trained with 20-step input — pad shorter sequences
PRED_STEPS  = 12      # future steps to predict
HIDDEN      = 128
LAYERS      = 2
INPUT_DIM   = 5       # dx_norm, dy_norm, speed_norm, sin(hdg), cos(hdg)
POS_SCALE   = 5_000.0 # metres — normalise position offsets
SPD_SCALE   = 150.0   # m/s   — normalise speed

MODEL_PATH  = os.path.join(os.path.dirname(__file__), "trajectory_model.pt")
_R_EARTH    = 6_371_000.0

# ── Coordinate helpers ────────────────────────────────────────────────────────

def _to_xy(lat: float, lon: float, lat0: float, lon0: float):
    """Equirectangular (lat,lon) → (x,y) metres relative to origin."""
    cos0 = math.cos(math.radians(lat0))
    x = math.radians(lon - lon0) * _R_EARTH * cos0
    y = math.radians(lat - lat0) * _R_EARTH
    return x, y


def _to_ll(x: float, y: float, lat0: float, lon0: float):
    """(x,y) metres → (lat,lon)."""
    cos0 = math.cos(math.radians(lat0))
    lat = lat0 + math.degrees(y / _R_EARTH)
    lon = lon0 + math.degrees(x / (_R_EARTH * cos0))
    return lat, lon


# ── Model ─────────────────────────────────────────────────────────────────────

if _TORCH_OK:
    class _TrajLSTM(nn.Module):
        """Sequence-to-point LSTM: (SEQ_LEN, INPUT_DIM) → (PRED_STEPS, 2)."""

        def __init__(self):
            super().__init__()
            self.lstm = nn.LSTM(
                INPUT_DIM, HIDDEN, LAYERS,
                batch_first=True, dropout=0.1,
            )
            self.head = nn.Sequential(
                nn.Linear(HIDDEN, 64),
                nn.ReLU(),
                nn.Linear(64, PRED_STEPS * 2),
            )

        def forward(self, x):                          # x: [B, SEQ_LEN, 5]
            out, _ = self.lstm(x)                      # [B, SEQ_LEN, H]
            return self.head(out[:, -1, :]).view(-1, PRED_STEPS, 2)


# ── Predictor singleton ───────────────────────────────────────────────────────

class _TrajectoryPredictor:
    def __init__(self):
        # {track_id: deque of {lat, lon, speed, heading, t}}
        self._hist: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=SEQ_LEN + 10)
        )
        self._model  = None
        self._ready  = False

        if not _TORCH_OK:
            return

        self._model = _TrajLSTM()
        if os.path.exists(MODEL_PATH):
            try:
                state = torch.load(MODEL_PATH, map_location="cpu", weights_only=True)
                self._model.load_state_dict(state)
                self._model.eval()
                self._ready = True
                log.info("[trajectory] Model loaded — LSTM trajectory active")
            except Exception as exc:
                log.warning("[trajectory] Model load failed: %s — run train_trajectory.py", exc)
        else:
            log.info("[trajectory] No model file — run train_trajectory.py to enable LSTM")

    # ── Public API ────────────────────────────────────────────────────────────

    def update(self, track_id: str, lat: float, lon: float,
               speed: float = 0.0, heading: float = 0.0) -> None:
        self._hist[track_id].append(
            {"lat": lat, "lon": lon, "speed": speed, "heading": heading, "t": time.time()}
        )

    def predict(self, track_id: str) -> Optional[List[dict]]:
        """Return list of predicted waypoints or None."""
        if not self._ready:
            return None
        hist = list(self._hist.get(track_id, []))
        if len(hist) < SEQ_LEN:
            return None

        # Use up to MODEL_SEQ most recent points
        hist = hist[-MODEL_SEQ:]
        lat0, lon0 = hist[-1]["lat"], hist[-1]["lon"]

        # Build normalised feature sequence
        seq = []
        for pt in hist:
            x, y   = _to_xy(pt["lat"], pt["lon"], lat0, lon0)
            hdg_r  = math.radians(pt.get("heading", 0.0))
            seq.append([
                x / POS_SCALE,
                y / POS_SCALE,
                pt.get("speed", 0.0) / SPD_SCALE,
                math.sin(hdg_r),
                math.cos(hdg_r),
            ])

        # Pad to MODEL_SEQ by repeating the first observation
        while len(seq) < MODEL_SEQ:
            seq.insert(0, seq[0])

        with torch.no_grad():
            x_t   = torch.tensor([seq], dtype=torch.float32)   # [1, MODEL_SEQ, 5]
            preds  = self._model(x_t).squeeze(0).numpy()        # [PRED, 2]

        # Denormalise
        preds *= POS_SCALE

        # Infer per-step interval from history
        dt = max(0.5, (hist[-1]["t"] - hist[0]["t"]) / max(len(hist) - 1, 1))

        last_t = hist[-1]["t"]
        waypoints = []
        for i, (px, py) in enumerate(preds):
            plat, plon = _to_ll(float(px), float(py), lat0, lon0)
            waypoints.append({
                "lat":  round(plat, 6),
                "lon":  round(plon, 6),
                "step": i + 1,
                "t":    last_t + (i + 1) * dt,
            })
        return waypoints

    def drop_track(self, track_id: str) -> None:
        self._hist.pop(track_id, None)

    def clear(self) -> None:
        self._hist.clear()

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def track_count(self) -> int:
        return len(self._hist)


# ── Module-level API (matches existing ai/* pattern) ─────────────────────────

_predictor = _TrajectoryPredictor()


def update(track_id: str, lat: float, lon: float,
           speed: float = 0.0, heading: float = 0.0) -> None:
    _predictor.update(track_id, lat, lon, speed, heading)


def predict(track_id: str) -> Optional[List[dict]]:
    return _predictor.predict(track_id)


def drop_track(track_id: str) -> None:
    _predictor.drop_track(track_id)


def clear() -> None:
    _predictor.clear()


def is_ready() -> bool:
    return _predictor.ready


def stats() -> dict:
    return {
        "ready":       _predictor.ready,
        "torch_ok":    _TORCH_OK,
        "tracks":      _predictor.track_count,
        "seq_len":     SEQ_LEN,
        "pred_steps":  PRED_STEPS,
        "model_path":  MODEL_PATH,
    }
