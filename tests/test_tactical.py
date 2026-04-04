"""
tests/test_tactical.py — Tests for ai/tactical.py
"""
import pytest
from ai import tactical


@pytest.fixture(autouse=True)
def _reset():
    tactical.reset()
    yield
    tactical.reset()


# ── Helper tests ─────────────────────────────────────────────────────────

class TestHelpers:
    def test_dist_m(self):
        assert tactical._dist_m(41.0, 29.0, 41.0, 29.0) == 0.0
        assert tactical._dist_m(41.0, 29.0, 41.001, 29.0) > 0

    def test_bearing_label(self):
        b = tactical._bearing(41.0, 29.0, 42.0, 29.0)
        assert b == "N"

    def test_bearing_east(self):
        b = tactical._bearing(41.0, 29.0, 41.0, 30.0)
        assert b == "E"

    def test_polygon_centroid(self):
        coords = [[0, 0], [0, 2], [2, 2], [2, 0]]
        lat, lon = tactical._polygon_centroid(coords)
        assert abs(lat - 1.0) < 0.01
        assert abs(lon - 1.0) < 0.01


# ── Recommendation generation ────────────────────────────────────────────

class TestGenerateRecommendations:
    def test_intercept_high_threat(self, sample_tracks, sample_threats,
                                   sample_assets, sample_zones):
        """HIGH threat near friendly → INTERCEPT recommendation."""
        recs = tactical.generate_recommendations(
            sample_tracks, sample_threats, sample_assets,
            sample_zones, [], {}
        )
        intercepts = [r for r in recs if r["type"] == "INTERCEPT"]
        # T-001 is HIGH and near assets
        assert len(intercepts) >= 1
        assert intercepts[0]["priority"] == 1

    def test_zone_warning(self, sample_zones, sample_assets):
        """Threat near restricted zone → ZONE_WARNING."""
        tracks = {
            "T-X": {
                "id": "T-X", "lat": 41.0105, "lon": 28.980,
                "speed": 20.0, "heading": 0.0,
                "threat_level": "HIGH",
            }
        }
        threats = {"T-X": {"threat_level": "HIGH", "score": 80}}
        recs = tactical.generate_recommendations(
            tracks, threats, sample_assets, sample_zones, [], {}
        )
        zone_warnings = [r for r in recs if r["type"] == "ZONE_WARNING"]
        assert len(zone_warnings) >= 1

    def test_escalate_on_swarm(self, sample_tracks, sample_threats,
                               sample_assets, sample_zones):
        """SWARM_DETECTED anomaly → ESCALATE recommendation."""
        anomalies = [{
            "type": "SWARM_DETECTED",
            "severity": "CRITICAL",
            "track_ids": ["T-001", "T-002", "T-003"],
            "count": 3,
        }]
        recs = tactical.generate_recommendations(
            sample_tracks, sample_threats, sample_assets,
            sample_zones, anomalies, {}
        )
        escalations = [r for r in recs if r["type"] == "ESCALATE"]
        assert len(escalations) >= 1

    def test_escalate_on_intent_shift(self, sample_tracks, sample_threats,
                                      sample_assets, sample_zones):
        """INTENT_SHIFT anomaly → ESCALATE recommendation."""
        anomalies = [{
            "type": "INTENT_SHIFT",
            "severity": "CRITICAL",
            "track_id": "T-001",
            "detail": "loitering -> attack",
        }]
        recs = tactical.generate_recommendations(
            sample_tracks, sample_threats, sample_assets,
            sample_zones, anomalies, {}
        )
        intent_esc = [r for r in recs
                      if r["type"] == "ESCALATE"
                      and r.get("anomaly_type") == "INTENT_SHIFT"]
        assert len(intent_esc) >= 1

    def test_withdraw_from_kill_zone(self, sample_zones):
        """Friendly asset in kill zone → WITHDRAW recommendation."""
        # zone-kill: [41.025-41.030, 28.980-28.990]
        assets = {
            "a1": {
                "id": "a1", "name": "Drone Unit",
                "type": "friendly", "status": "active",
                "lat": 41.027, "lon": 28.985,
            }
        }
        recs = tactical.generate_recommendations(
            {}, {}, assets, sample_zones, [], {}
        )
        withdrawals = [r for r in recs if r["type"] == "WITHDRAW"]
        assert len(withdrawals) >= 1

    def test_monitor_uncovered_medium(self, sample_zones):
        """MEDIUM threat without coverage → MONITOR recommendation."""
        tracks = {
            "T-M": {
                "id": "T-M", "lat": 41.100, "lon": 29.100,
                "speed": 20.0, "threat_level": "MEDIUM",
            }
        }
        threats = {"T-M": {"threat_level": "MEDIUM", "score": 50}}
        # No nearby friendlies
        assets = {
            "far": {
                "id": "far", "name": "Far Base",
                "type": "friendly", "status": "active",
                "lat": 40.800, "lon": 28.500,
            }
        }
        recs = tactical.generate_recommendations(
            tracks, threats, assets, sample_zones, [], {}
        )
        monitors = [r for r in recs if r["type"] == "MONITOR"]
        assert len(monitors) >= 1

    def test_results_sorted_by_priority(self, sample_tracks, sample_threats,
                                        sample_assets, sample_zones):
        recs = tactical.generate_recommendations(
            sample_tracks, sample_threats, sample_assets,
            sample_zones, [], {}
        )
        for i in range(1, len(recs)):
            assert recs[i]["priority"] >= recs[i - 1]["priority"]

    def test_empty_inputs_no_crash(self):
        recs = tactical.generate_recommendations({}, {}, {}, {}, [], {})
        assert recs == []
