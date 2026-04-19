"""Kalman engine tests — tahmin, güncelleme, Mahalanobis."""
from __future__ import annotations

import numpy as np
import pytest

from services.fusion.kf_engine import (
    mahalanobis_distance,
    make_cv_filter,
    predict,
    update,
)


def test_initial_state():
    kf = make_cv_filter(10.0, 20.0, 30.0)
    assert kf.x[0] == 10.0
    assert kf.x[1] == 20.0
    assert kf.x[2] == 30.0
    assert kf.x[3] == 0.0  # velocity starts at 0


def test_predict_moves_state_with_velocity():
    kf = make_cv_filter(0.0, 0.0, 0.0)
    kf.x[3] = 5.0   # vx = 5 m/s
    predict(kf, dt=1.0)
    assert kf.x[0] == pytest.approx(5.0)


def test_update_pulls_state_toward_measurement():
    kf = make_cv_filter(0.0, 0.0, 0.0, sigma_pos=100.0)
    z = np.array([50.0, 0.0, 0.0])
    update(kf, z, measurement_sigma=1.0)
    assert 0.0 < kf.x[0] <= 50.0
    assert kf.x[0] > 40.0  # güçlü çekim


def test_mahalanobis_close_measurement_small():
    kf = make_cv_filter(0.0, 0.0, 0.0, sigma_pos=5.0)
    z = np.array([0.1, 0.1, 0.0])
    d = mahalanobis_distance(kf, z)
    assert d < 0.1


def test_mahalanobis_far_measurement_large():
    kf = make_cv_filter(0.0, 0.0, 0.0, sigma_pos=5.0)
    z = np.array([500.0, 0.0, 0.0])
    d = mahalanobis_distance(kf, z)
    assert d > 10.0
