"""IMM filter tests."""
from __future__ import annotations

import numpy as np

from services.fusion.imm_engine import imm_mode_probabilities, make_imm_filter


def test_imm_returns_two_model_mixture():
    imm = make_imm_filter(0.0, 0.0, 0.0)
    probs = imm_mode_probabilities(imm)
    assert len(probs) == 2
    assert abs(sum(probs) - 1.0) < 1e-6


def test_imm_predict_and_update_runs():
    imm = make_imm_filter(0.0, 0.0, 0.0)
    imm.predict()
    imm.update(np.array([1.0, 1.0, 0.0]))
    assert imm.x is not None


def test_imm_initial_state_centered_on_input():
    imm = make_imm_filter(100.0, 200.0, 50.0)
    assert imm.x[0] == 100.0
    assert imm.x[1] == 200.0
    assert imm.x[2] == 50.0
