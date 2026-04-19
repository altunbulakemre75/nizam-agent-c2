"""Hungarian + Mahalanobis association tests."""
from __future__ import annotations

import numpy as np

from services.fusion.association import associate
from services.fusion.kf_engine import make_cv_filter


def test_empty_tracks_and_measurements():
    matches, u_t, u_m = associate([], [])
    assert matches == [] and u_t == [] and u_m == []


def test_no_tracks_all_measurements_unmatched():
    measurements = [np.array([0.0, 0.0, 0.0]), np.array([100.0, 100.0, 0.0])]
    matches, u_t, u_m = associate([], measurements)
    assert matches == []
    assert u_t == []
    assert u_m == [0, 1]


def test_one_track_one_close_measurement_matches():
    kf = make_cv_filter(0.0, 0.0, 0.0, sigma_pos=5.0)
    matches, u_t, u_m = associate([kf], [np.array([1.0, 1.0, 0.0])])
    assert matches == [(0, 0)]
    assert u_t == [] and u_m == []


def test_far_measurement_outside_gate_unmatched():
    kf = make_cv_filter(0.0, 0.0, 0.0, sigma_pos=5.0)
    far = np.array([1000.0, 0.0, 0.0])  # Mahalanobis >> gate
    matches, u_t, u_m = associate([kf], [far])
    assert matches == []
    assert u_t == [0]
    assert u_m == [0]


def test_optimal_two_two_assignment():
    kf1 = make_cv_filter(0.0, 0.0, 0.0, sigma_pos=5.0)
    kf2 = make_cv_filter(100.0, 0.0, 0.0, sigma_pos=5.0)
    measurements = [
        np.array([100.0, 0.0, 0.0]),  # eşleşir kf2
        np.array([0.0, 0.0, 0.0]),    # eşleşir kf1
    ]
    matches, u_t, u_m = associate([kf1, kf2], measurements)
    matches_set = {(t, m) for t, m in matches}
    assert matches_set == {(0, 1), (1, 0)}
    assert u_t == [] and u_m == []
