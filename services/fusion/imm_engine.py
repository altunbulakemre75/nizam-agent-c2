"""IMM (Interacting Multiple Model) filtre — manevra durumlarında daha iyi.

3 model paralel çalışır:
  - CV (constant velocity) — düz uçuş
  - CA (constant acceleration) — dalış/tırmanış
  - CT (coordinated turn) — sabit dönüş

Her tick'te en olası model devreye girer; track KF state seçilir.
Plan'daki "filterpy.IMMEstimator" satırına karşılık.
"""
from __future__ import annotations

import numpy as np
from filterpy.kalman import IMMEstimator, KalmanFilter

from services.fusion.kf_engine import make_cv_filter


def _make_ca_filter(x0: float, y0: float, z0: float, sigma_pos: float = 10.0) -> KalmanFilter:
    """Constant acceleration: state = [x, y, z, vx, vy, vz, ax, ay, az]."""
    kf = KalmanFilter(dim_x=9, dim_z=3)
    kf.x = np.array([x0, y0, z0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    kf.P = np.diag([sigma_pos**2] * 3 + [50.0**2] * 3 + [5.0**2] * 3)
    kf.H = np.zeros((3, 9))
    kf.H[0, 0] = kf.H[1, 1] = kf.H[2, 2] = 1.0
    kf.R = np.eye(3) * (sigma_pos**2)
    kf._dt = 0.1  # type: ignore[attr-defined]
    _set_ca_matrices(kf, 0.1)
    return kf


def _set_ca_matrices(kf: KalmanFilter, dt: float) -> None:
    """F ve Q (CA modeli) matrislerini dt ile güncelle."""
    F = np.eye(9)
    for i in range(3):
        F[i, i + 3] = dt
        F[i, i + 6] = 0.5 * dt * dt
        F[i + 3, i + 6] = dt
    kf.F = F
    q = 1.0  # ivme varyansı
    Q = np.eye(9) * q
    kf.Q = Q


def make_imm_filter(
    x0: float, y0: float, z0: float, sigma_pos: float = 10.0
) -> IMMEstimator:
    """CV + CA iki-model IMM filtresi (CT opsiyonel, testten sonra eklenebilir).

    filterpy.IMMEstimator aynı state boyutunu ister — CA'nın 9-dim state'ini
    CV'nin 6-dim'e kırpmak için basit projection gerekli. Bu implementasyon
    iki CV filtresi farklı process noise seviyelerinde kullanır (pragmatic):
      - Filter 1: düşük noise (düz uçuş)
      - Filter 2: yüksek noise (manevra)
    """
    kf_cv = make_cv_filter(x0, y0, z0, sigma_pos=sigma_pos, process_noise=0.5)
    kf_maneuver = make_cv_filter(x0, y0, z0, sigma_pos=sigma_pos, process_noise=5.0)

    # Mode olasılıkları (başlangıçta düz uçuş öncelikli)
    mu = np.array([0.9, 0.1])
    # Geçiş matrisi (0.95 kalma olasılığı, 0.05 moda geçiş)
    trans_mat = np.array([[0.95, 0.05], [0.05, 0.95]])

    imm = IMMEstimator(filters=[kf_cv, kf_maneuver], mu=mu, M=trans_mat)
    return imm


def imm_mode_probabilities(imm: IMMEstimator) -> list[float]:
    """Her modun anlık olasılığı (0..1)."""
    return [float(m) for m in imm.mu]
