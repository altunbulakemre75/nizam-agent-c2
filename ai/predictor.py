"""
ai/predictor.py  —  Kalman-filter track predictor

Maintains a 2-D constant-velocity Kalman filter per track.
On each measurement (lat, lon, timestamp), the filter updates and
produces N predicted future positions.

Pure Python — no numpy dependency.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ── Tiny matrix helpers (lists-of-lists) ────────────────────────────────────

Vec = List[float]
Mat = List[List[float]]


def _zeros(r: int, c: int) -> Mat:
    return [[0.0] * c for _ in range(r)]


def _eye(n: int) -> Mat:
    m = _zeros(n, n)
    for i in range(n):
        m[i][i] = 1.0
    return m


def _T(A: Mat) -> Mat:
    r, c = len(A), len(A[0])
    return [[A[i][j] for i in range(r)] for j in range(c)]


def _add(A: Mat, B: Mat) -> Mat:
    return [[A[i][j] + B[i][j] for j in range(len(A[0]))] for i in range(len(A))]


def _sub(A: Mat, B: Mat) -> Mat:
    return [[A[i][j] - B[i][j] for j in range(len(A[0]))] for i in range(len(A))]


def _mul(A: Mat, B: Mat) -> Mat:
    rA, cA, cB = len(A), len(A[0]), len(B[0])
    C = _zeros(rA, cB)
    for i in range(rA):
        for j in range(cB):
            s = 0.0
            for k in range(cA):
                s += A[i][k] * B[k][j]
            C[i][j] = s
    return C


def _scale(A: Mat, s: float) -> Mat:
    return [[A[i][j] * s for j in range(len(A[0]))] for i in range(len(A))]


def _inv2(M: Mat) -> Mat:
    """Invert a 2x2 matrix."""
    a, b = M[0][0], M[0][1]
    c, d = M[1][0], M[1][1]
    det = a * d - b * c
    if abs(det) < 1e-30:
        det = 1e-30
    inv_det = 1.0 / det
    return [[d * inv_det, -b * inv_det],
            [-c * inv_det, a * inv_det]]


def _col(v: Vec) -> Mat:
    """Column vector from flat list."""
    return [[x] for x in v]


def _flat(col: Mat) -> Vec:
    """Flat list from column vector."""
    return [row[0] for row in col]


# ── Kalman filter ───────────────────────────────────────────────────────────

@dataclass
class _KF:
    """4-state (lat, lon, vlat, vlon) constant-velocity Kalman filter."""
    x: Mat                          # state [4x1]
    P: Mat                          # covariance [4x4]
    last_t: float                   # last measurement epoch (unix seconds)
    q_std: float = 3e-6             # process noise std (degrees/s^2)
    r_std: float = 7e-5             # measurement noise std (degrees)

    def predict(self, dt: float) -> None:
        F = _eye(4)
        F[0][2] = dt
        F[1][3] = dt
        self.x = _mul(F, self.x)
        q = self.q_std ** 2
        # Discrete-time process noise for constant-velocity model
        dt2 = dt * dt
        dt3 = dt2 * dt / 2.0
        dt4 = dt2 * dt2 / 4.0
        Q = _zeros(4, 4)
        Q[0][0] = dt4 * q;  Q[0][2] = dt3 * q
        Q[1][1] = dt4 * q;  Q[1][3] = dt3 * q
        Q[2][0] = dt3 * q;  Q[2][2] = dt2 * q
        Q[3][1] = dt3 * q;  Q[3][3] = dt2 * q
        self.P = _add(_mul(_mul(F, self.P), _T(F)), Q)

    def update(self, z_lat: float, z_lon: float) -> None:
        H = [[1, 0, 0, 0],
             [0, 1, 0, 0]]
        R = [[self.r_std ** 2, 0],
             [0, self.r_std ** 2]]
        z = _col([z_lat, z_lon])
        y = _sub(z, _mul(H, self.x))                   # innovation
        S = _add(_mul(_mul(H, self.P), _T(H)), R)      # innovation cov
        S_inv = _inv2(S)
        K = _mul(_mul(self.P, _T(H)), S_inv)            # Kalman gain [4x2]
        self.x = _add(self.x, _mul(K, y))
        IKH = _sub(_eye(4), _mul(K, H))
        self.P = _mul(IKH, self.P)

    def extrapolate(self, steps: int, dt: float) -> List[Tuple[float, float]]:
        """Return predicted (lat, lon) for next `steps` time increments."""
        F = _eye(4)
        F[0][2] = dt
        F[1][3] = dt
        x_cur = [row[:] for row in self.x]
        preds = []
        for _ in range(steps):
            x_cur = _mul(F, x_cur)
            preds.append((x_cur[0][0], x_cur[1][0]))
        return preds

    def extrapolate_with_uncertainty(self, steps: int, dt: float
                                     ) -> List[Tuple[float, float, float, float]]:
        """Return predicted (lat, lon, sigma_lat, sigma_lon) for each step.

        sigma values are 1-sigma standard deviations in degrees, derived
        from propagating the covariance matrix through the state transition.
        """
        F = _eye(4)
        F[0][2] = dt
        F[1][3] = dt

        q = self.q_std ** 2
        dt2 = dt * dt
        dt3 = dt2 * dt / 2.0
        dt4 = dt2 * dt2 / 4.0
        Q = _zeros(4, 4)
        Q[0][0] = dt4 * q;  Q[0][2] = dt3 * q
        Q[1][1] = dt4 * q;  Q[1][3] = dt3 * q
        Q[2][0] = dt3 * q;  Q[2][2] = dt2 * q
        Q[3][1] = dt3 * q;  Q[3][3] = dt2 * q

        x_cur = [row[:] for row in self.x]
        P_cur = [row[:] for row in self.P]
        preds = []
        for _ in range(steps):
            x_cur = _mul(F, x_cur)
            P_cur = _add(_mul(_mul(F, P_cur), _T(F)), Q)
            sigma_lat = math.sqrt(max(P_cur[0][0], 0.0))
            sigma_lon = math.sqrt(max(P_cur[1][1], 0.0))
            preds.append((x_cur[0][0], x_cur[1][0], sigma_lat, sigma_lon))
        return preds


# ── Public API ──────────────────────────────────────────────────────────────

# Per-track Kalman filter instances
_filters: Dict[str, _KF] = {}

# How many seconds ahead to predict, and the step size
PREDICT_HORIZON_S = 60
PREDICT_STEP_S    = 5
PREDICT_STEPS     = PREDICT_HORIZON_S // PREDICT_STEP_S  # 12 points


def update_track(track_id: str, lat: float, lon: float,
                 ts: Optional[float] = None) -> List[Dict]:
    """
    Feed a new measurement for *track_id* and return predicted trajectory.

    Returns list of dicts:
      [{"lat": ..., "lon": ..., "time_ahead_s": 5}, ...]
    """
    now = ts or time.time()

    kf = _filters.get(track_id)
    if kf is None:
        # Initialize filter at first observation
        kf = _KF(
            x=_col([lat, lon, 0.0, 0.0]),
            P=_scale(_eye(4), 1e-6),
            last_t=now,
        )
        _filters[track_id] = kf
        return []

    dt = now - kf.last_t
    if dt < 0.05:
        return []  # ignore duplicate / too-fast updates

    kf.predict(dt)
    kf.update(lat, lon)
    kf.last_t = now

    preds = kf.extrapolate_with_uncertainty(PREDICT_STEPS, PREDICT_STEP_S)
    return [
        {"lat": round(p[0], 7), "lon": round(p[1], 7),
         "sigma_lat": round(p[2], 9), "sigma_lon": round(p[3], 9),
         "time_ahead_s": (i + 1) * PREDICT_STEP_S}
        for i, p in enumerate(preds)
    ]


def get_velocity(track_id: str) -> Optional[Tuple[float, float]]:
    """Return current estimated (vlat, vlon) in deg/s, or None."""
    kf = _filters.get(track_id)
    if kf is None:
        return None
    return (kf.x[2][0], kf.x[3][0])


def remove_track(track_id: str) -> None:
    _filters.pop(track_id, None)


def reset() -> None:
    _filters.clear()
