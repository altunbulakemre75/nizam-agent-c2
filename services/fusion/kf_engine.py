"""3D sabit-hız Kalman filtresi — filterpy tabanlı.

State:  [x, y, z, vx, vy, vz]  (ENU metre, m/s)
Ölçüm:  [x, y, z]              (ENU metre)

Sabit-hız model her track için bağımsız. IMM gelecekte eklenebilir
(manevra durumlarında CA/CT filtreleri).
"""
from __future__ import annotations

import numpy as np
from filterpy.kalman import KalmanFilter


def make_cv_filter(
    x0: float,
    y0: float,
    z0: float,
    sigma_pos: float = 10.0,
    sigma_vel: float = 50.0,
    process_noise: float = 1.0,
) -> KalmanFilter:
    """Yeni bir sabit-hız 3D KF oluştur.

    Args:
        x0, y0, z0: ilk konum (metre)
        sigma_pos: ilk konum belirsizliği (1-sigma metre)
        sigma_vel: ilk hız belirsizliği (1-sigma m/s)
        process_noise: sürece eklenecek ivme gürültüsü (m/s^2)
    """
    kf = KalmanFilter(dim_x=6, dim_z=3)
    kf.x = np.array([x0, y0, z0, 0.0, 0.0, 0.0])
    kf.P = np.diag([sigma_pos**2] * 3 + [sigma_vel**2] * 3)
    kf.H = np.zeros((3, 6))
    kf.H[0, 0] = kf.H[1, 1] = kf.H[2, 2] = 1.0
    kf.R = np.eye(3) * (sigma_pos**2)
    kf._process_noise_std = process_noise  # type: ignore[attr-defined]
    _set_transition(kf, dt=0.1)
    return kf


def _set_transition(kf: KalmanFilter, dt: float) -> None:
    """F (geçiş) ve Q (süreç gürültüsü) matrislerini dt ile güncelle."""
    F = np.eye(6)
    F[0, 3] = F[1, 4] = F[2, 5] = dt
    kf.F = F

    q = getattr(kf, "_process_noise_std", 1.0)
    # Discrete white noise acceleration (DWNA) modeli
    dt2 = dt * dt
    dt3 = dt2 * dt
    dt4 = dt2 * dt2
    Q_block = np.array([[dt4 / 4.0, dt3 / 2.0], [dt3 / 2.0, dt2]]) * (q * q)
    Q = np.zeros((6, 6))
    for i in range(3):
        Q[i, i] = Q_block[0, 0]
        Q[i, i + 3] = Q_block[0, 1]
        Q[i + 3, i] = Q_block[1, 0]
        Q[i + 3, i + 3] = Q_block[1, 1]
    kf.Q = Q


def predict(kf: KalmanFilter, dt: float) -> None:
    _set_transition(kf, dt)
    kf.predict()


def update(kf: KalmanFilter, z: np.ndarray, measurement_sigma: float | None = None) -> None:
    """Ölçümle güncelle. z = [x, y, z]."""
    if measurement_sigma is not None:
        kf.R = np.eye(3) * (measurement_sigma**2)
    kf.update(z)


def mahalanobis_distance(kf: KalmanFilter, z: np.ndarray) -> float:
    """Mahalanobis mesafesi — ölçüm track'e ait olma ihtimali."""
    y = z - kf.H @ kf.x
    S = kf.H @ kf.P @ kf.H.T + kf.R
    S_inv = np.linalg.inv(S)
    dist_sq = float(y.T @ S_inv @ y)
    return float(np.sqrt(max(dist_sq, 0.0)))
