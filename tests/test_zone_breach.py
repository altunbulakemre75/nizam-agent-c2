"""
tests/test_zone_breach.py — Tests for ai/zone_breach.py
"""
import math
import pytest
from ai import zone_breach as zb


@pytest.fixture(autouse=True)
def _reset():
    zb.reset()
    yield
    zb.reset()


# ── Helper tests ─────────────────────────────────────────────────────────

class TestPointInPolygon:
    def test_inside(self):
        coords = [[0, 0], [0, 1], [1, 1], [1, 0]]
        assert zb._point_in_polygon(0.5, 0.5, coords) is True

    def test_outside(self):
        coords = [[0, 0], [0, 1], [1, 1], [1, 0]]
        assert zb._point_in_polygon(2.0, 2.0, coords) is False

    def test_too_few(self):
        assert zb._point_in_polygon(0.5, 0.5, [[0, 0]]) is False


class TestNearestDist:
    def test_inside_returns_zero(self):
        coords = [[0, 0], [0, 1], [1, 1], [1, 0]]
        assert zb._nearest_polygon_dist_m(0.5, 0.5, coords) == 0.0

    def test_outside_positive(self):
        coords = [[0, 0], [0, 1], [1, 1], [1, 0]]
        d = zb._nearest_polygon_dist_m(2.0, 2.0, coords)
        assert d > 0


class TestEllipseIntersects:
    def test_center_inside(self):
        coords = [[0, 0], [0, 1], [1, 1], [1, 0]]
        assert zb._ellipse_intersects_polygon(0.5, 0.5, 0.01, 0.01, coords) is True

    def test_ellipse_touches_polygon(self):
        coords = [[0, 0], [0, 1], [1, 1], [1, 0]]
        # Center outside but large sigma reaches in
        assert zb._ellipse_intersects_polygon(1.5, 0.5, 1.0, 1.0, coords) is True

    def test_ellipse_too_small(self):
        coords = [[0, 0], [0, 1], [1, 1], [1, 0]]
        # Far outside with tiny sigma
        assert zb._ellipse_intersects_polygon(5.0, 5.0, 0.001, 0.001, coords) is False


# ── Breach prediction tests ─────────────────────────────────────────────

class TestPredictiveBreaches:
    def test_no_predictions_no_breach(self, sample_zones):
        result = zb.check_predictive_breaches({}, sample_zones)
        assert result == []

    def test_no_zones_no_breach(self):
        predictions = {"T1": [{"lat": 41.0, "lon": 29.0,
                               "sigma_lat": 0.0, "sigma_lon": 0.0,
                               "time_ahead_s": 5}]}
        result = zb.check_predictive_breaches(predictions, {})
        assert result == []

    def test_breach_detected(self, sample_zones):
        """Prediction entering restricted zone triggers breach."""
        # zone-restricted: [41.010-41.020, 28.970-28.990]
        predictions = {
            "T1": [
                {"lat": 41.015, "lon": 28.980,
                 "sigma_lat": 0.0001, "sigma_lon": 0.0001,
                 "time_ahead_s": 10},
            ]
        }
        result = zb.check_predictive_breaches(predictions, sample_zones)
        assert len(result) >= 1
        assert result[0]["type"] == "PREDICTIVE_BREACH"
        assert result[0]["track_id"] == "T1"

    def test_kill_zone_breach_critical(self, sample_zones):
        """Kill zone breach has CRITICAL severity."""
        # zone-kill: [41.025-41.030, 28.980-28.990]
        predictions = {
            "T1": [
                {"lat": 41.027, "lon": 28.985,
                 "sigma_lat": 0.0001, "sigma_lon": 0.0001,
                 "time_ahead_s": 5},
            ]
        }
        result = zb.check_predictive_breaches(predictions, sample_zones)
        kill_breaches = [b for b in result if b.get("zone_type") == "kill"]
        assert len(kill_breaches) >= 1
        assert kill_breaches[0]["severity"] == "CRITICAL"

    def test_no_breach_outside(self, sample_zones):
        """Predictions outside all zones → no breach."""
        predictions = {
            "T1": [
                {"lat": 41.100, "lon": 29.100,
                 "sigma_lat": 0.00001, "sigma_lon": 0.00001,
                 "time_ahead_s": 10},
            ]
        }
        result = zb.check_predictive_breaches(predictions, sample_zones)
        assert result == []

    def test_sorted_by_time(self, sample_zones):
        """Breaches should be sorted earliest first."""
        predictions = {
            "T1": [
                {"lat": 41.015, "lon": 28.980,
                 "sigma_lat": 0.0001, "sigma_lon": 0.0001,
                 "time_ahead_s": 30},
            ],
            "T2": [
                {"lat": 41.027, "lon": 28.985,
                 "sigma_lat": 0.0001, "sigma_lon": 0.0001,
                 "time_ahead_s": 10},
            ],
        }
        result = zb.check_predictive_breaches(predictions, sample_zones)
        if len(result) >= 2:
            assert result[0]["time_to_breach_s"] <= result[1]["time_to_breach_s"]


# ── Uncertainty cones ────────────────────────────────────────────────────

class TestUncertaintyCones:
    def test_build_cones(self):
        predictions = {
            "T1": [
                {"lat": 41.0, "lon": 29.0,
                 "sigma_lat": 0.001, "sigma_lon": 0.002,
                 "time_ahead_s": 5},
                {"lat": 41.001, "lon": 29.001,
                 "sigma_lat": 0.002, "sigma_lon": 0.004,
                 "time_ahead_s": 10},
            ]
        }
        cones = zb.build_uncertainty_cones(predictions)
        assert "T1" in cones
        assert len(cones["T1"]) == 2
        assert "sigma_lat_m" in cones["T1"][0]
        assert "sigma_lon_m" in cones["T1"][0]
        assert cones["T1"][0]["sigma_lat_m"] > 0

    def test_empty_predictions(self):
        assert zb.build_uncertainty_cones({}) == {}
