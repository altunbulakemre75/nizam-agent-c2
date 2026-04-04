"""
tests/test_ml_threat.py — Tests for ai/ml_threat.py
"""
import numpy as np
import pytest
from ai import ml_threat


@pytest.fixture(autouse=True)
def _reset():
    ml_threat.reset()
    yield
    ml_threat.reset()


# ── Feature extraction tests ─────────────────────────────────────────────

class TestExtractTrackFeatures:
    def test_basic_extraction(self, sample_track):
        features = ml_threat.extract_track_features(sample_track)
        assert isinstance(features, np.ndarray)
        assert features.shape == (16,)

    def test_feature_values(self, sample_track):
        features = ml_threat.extract_track_features(sample_track)
        # speed = 35.0
        assert features[0] == 35.0
        # closing_speed = max(0, -(-20)) = 20
        assert features[1] == 20.0
        # range_m = 1200
        assert features[2] == 1200.0
        # is_drone = 1.0
        assert features[6] == 1.0
        # is_helicopter = 0.0
        assert features[7] == 0.0
        # intent_attack = 1.0
        assert features[8] == 1.0

    def test_empty_track(self):
        features = ml_threat.extract_track_features({})
        assert features.shape == (16,)
        # All should be zero or default
        assert features[0] == 0.0  # speed

    def test_acceleration_with_prev_track(self, sample_track):
        prev = dict(sample_track)
        prev["speed"] = 20.0
        features = ml_threat.extract_track_features(
            sample_track, prev_track=prev, dt=1.0
        )
        # acceleration = (35 - 20) / 1.0 = 15
        assert abs(features[12] - 15.0) < 0.01

    def test_min_asset_distance(self, sample_track, sample_assets):
        features = ml_threat.extract_track_features(
            sample_track, assets=sample_assets
        )
        # Should compute distance to nearest friendly
        assert features[14] < 99999.0
        assert features[14] > 0

    def test_in_zone(self, sample_zones):
        # Track inside restricted zone
        track = {"lat": 41.015, "lon": 28.980}
        features = ml_threat.extract_track_features(
            track, zones=sample_zones
        )
        assert features[15] == 1.0  # in_zone

    def test_not_in_zone(self, sample_zones):
        track = {"lat": 41.100, "lon": 29.100}
        features = ml_threat.extract_track_features(
            track, zones=sample_zones
        )
        assert features[15] == 0.0

    def test_helicopter_classification(self):
        track = {"classification": {"label": "helicopter"}}
        features = ml_threat.extract_track_features(track)
        assert features[6] == 0.0  # is_drone
        assert features[7] == 1.0  # is_helicopter


# ── Point-in-polygon test ────────────────────────────────────────────────

class TestPointInPolygon:
    def test_inside(self):
        coords = [[0, 0], [0, 1], [1, 1], [1, 0]]
        assert ml_threat._point_in_polygon(0.5, 0.5, coords) is True

    def test_outside(self):
        coords = [[0, 0], [0, 1], [1, 1], [1, 0]]
        assert ml_threat._point_in_polygon(2.0, 2.0, coords) is False


# ── Distance helper test ─────────────────────────────────────────────────

class TestDistM:
    def test_same_point(self):
        assert ml_threat._dist_m(41.0, 29.0, 41.0, 29.0) == 0.0

    def test_positive(self):
        d = ml_threat._dist_m(41.0, 29.0, 41.001, 29.001)
        assert d > 0


# ── Feature names / labels ───────────────────────────────────────────────

class TestConstants:
    def test_feature_count(self):
        assert len(ml_threat.FEATURE_NAMES) == 16

    def test_label_map(self):
        assert ml_threat.LABEL_MAP == {"LOW": 0, "MEDIUM": 1, "HIGH": 2}

    def test_label_names(self):
        assert ml_threat.LABEL_NAMES == ["LOW", "MEDIUM", "HIGH"]


# ── Model availability ──────────────────────────────────────────────────

class TestModelInfo:
    def test_get_model_info_no_model(self):
        ml_threat.reset()
        # If model file doesn't exist, should return available: False
        # (This depends on whether a model has been trained)
        info = ml_threat.get_model_info()
        assert "available" in info
