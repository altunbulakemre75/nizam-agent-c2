"""
tests/test_predictor.py — Tests for ai/predictor.py (Kalman filter)
"""
import pytest
from ai import predictor


@pytest.fixture(autouse=True)
def _reset():
    predictor.reset()
    yield
    predictor.reset()


# ── Matrix helper tests ──────────────────────────────────────────────────

class TestMatrixHelpers:
    def test_zeros(self):
        m = predictor._zeros(2, 3)
        assert len(m) == 2 and len(m[0]) == 3
        assert all(m[i][j] == 0.0 for i in range(2) for j in range(3))

    def test_eye(self):
        m = predictor._eye(3)
        for i in range(3):
            for j in range(3):
                assert m[i][j] == (1.0 if i == j else 0.0)

    def test_transpose(self):
        A = [[1, 2, 3], [4, 5, 6]]
        T = predictor._T(A)
        assert T == [[1, 4], [2, 5], [3, 6]]

    def test_add_sub(self):
        A = [[1, 2], [3, 4]]
        B = [[5, 6], [7, 8]]
        assert predictor._add(A, B) == [[6, 8], [10, 12]]
        assert predictor._sub(A, B) == [[-4, -4], [-4, -4]]

    def test_mul_identity(self):
        A = [[1, 2], [3, 4]]
        I = predictor._eye(2)
        result = predictor._mul(A, I)
        assert result == A

    def test_mul(self):
        A = [[1, 2], [3, 4]]
        B = [[5, 6], [7, 8]]
        result = predictor._mul(A, B)
        assert result == [[19, 22], [43, 50]]

    def test_scale(self):
        A = [[1, 2], [3, 4]]
        result = predictor._scale(A, 2.0)
        assert result == [[2, 4], [6, 8]]

    def test_inv2(self):
        A = [[4, 7], [2, 6]]
        inv = predictor._inv2(A)
        # A * inv should be identity
        product = predictor._mul(A, inv)
        for i in range(2):
            for j in range(2):
                expected = 1.0 if i == j else 0.0
                assert abs(product[i][j] - expected) < 1e-10

    def test_col_flat_roundtrip(self):
        v = [1.0, 2.0, 3.0]
        col = predictor._col(v)
        assert col == [[1.0], [2.0], [3.0]]
        assert predictor._flat(col) == v


# ── Kalman filter tests ──────────────────────────────────────────────────

class TestKalmanFilter:
    def test_first_update_returns_empty(self):
        """First measurement initializes filter, no predictions yet."""
        preds = predictor.update_track("T1", 41.0, 29.0, ts=1000.0)
        assert preds == []

    def test_second_update_returns_predictions(self):
        predictor.update_track("T1", 41.0, 29.0, ts=1000.0)
        preds = predictor.update_track("T1", 41.001, 29.0, ts=1001.0)
        assert len(preds) == predictor.PREDICT_STEPS
        assert preds[0]["time_ahead_s"] == predictor.PREDICT_STEP_S

    def test_predictions_extrapolate_forward(self):
        """Moving north: predictions should increase in latitude."""
        predictor.update_track("T1", 41.0, 29.0, ts=1000.0)
        predictor.update_track("T1", 41.001, 29.0, ts=1001.0)
        preds = predictor.update_track("T1", 41.002, 29.0, ts=1002.0)
        # Each predicted lat should be >= the previous
        for i in range(1, len(preds)):
            assert preds[i]["lat"] >= preds[i - 1]["lat"]

    def test_predictions_include_uncertainty(self):
        predictor.update_track("T1", 41.0, 29.0, ts=1000.0)
        preds = predictor.update_track("T1", 41.001, 29.0, ts=1001.0)
        for p in preds:
            assert "sigma_lat" in p
            assert "sigma_lon" in p
            assert p["sigma_lat"] >= 0
            assert p["sigma_lon"] >= 0

    def test_uncertainty_grows_with_time(self):
        """Uncertainty should increase for later predictions."""
        predictor.update_track("T1", 41.0, 29.0, ts=1000.0)
        preds = predictor.update_track("T1", 41.001, 29.0, ts=1001.0)
        for i in range(1, len(preds)):
            assert preds[i]["sigma_lat"] >= preds[i - 1]["sigma_lat"]

    def test_get_velocity_none_before_init(self):
        assert predictor.get_velocity("nonexistent") is None

    def test_get_velocity_after_updates(self):
        predictor.update_track("T1", 41.0, 29.0, ts=1000.0)
        predictor.update_track("T1", 41.001, 29.0, ts=1001.0)
        vel = predictor.get_velocity("T1")
        assert vel is not None
        vlat, vlon = vel
        assert vlat > 0  # moving north

    def test_ignore_too_fast_updates(self):
        predictor.update_track("T1", 41.0, 29.0, ts=1000.0)
        # Same timestamp = too fast
        preds = predictor.update_track("T1", 41.001, 29.0, ts=1000.01)
        assert preds == []


# ── Lifecycle ────────────────────────────────────────────────────────────

class TestLifecycle:
    def test_remove_track(self):
        predictor.update_track("T1", 41.0, 29.0, ts=1000.0)
        assert "T1" in predictor._filters
        predictor.remove_track("T1")
        assert "T1" not in predictor._filters

    def test_reset(self):
        predictor.update_track("T1", 41.0, 29.0, ts=1000.0)
        predictor.update_track("T2", 42.0, 29.0, ts=1000.0)
        predictor.reset()
        assert len(predictor._filters) == 0
