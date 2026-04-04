"""
tests/test_roe.py — Tests for ai/roe.py (Rules of Engagement)
"""
import pytest
from ai import roe


@pytest.fixture(autouse=True)
def _reset():
    roe.reset()
    yield
    roe.reset()


# ── Helper tests ─────────────────────────────────────────────────────────

class TestPointInPolygon:
    def test_inside_square(self):
        coords = [[0, 0], [0, 1], [1, 1], [1, 0]]
        assert roe._point_in_polygon(0.5, 0.5, coords) is True

    def test_outside_square(self):
        coords = [[0, 0], [0, 1], [1, 1], [1, 0]]
        assert roe._point_in_polygon(2.0, 2.0, coords) is False

    def test_too_few_points(self):
        coords = [[0, 0], [1, 1]]
        assert roe._point_in_polygon(0.5, 0.5, coords) is False


class TestDistM:
    def test_same_point(self):
        assert roe._dist_m(41.0, 29.0, 41.0, 29.0) == 0.0

    def test_positive_distance(self):
        d = roe._dist_m(41.0, 29.0, 41.001, 29.001)
        assert d > 0


# ── ROE Decision Matrix tests ───────────────────────────────────────────

class TestEvaluateTrack:
    def _make_track(self, lat, lon, speed=30, intent="unknown",
                    threat_level="MEDIUM"):
        return {
            "lat": lat, "lon": lon, "speed": speed,
            "intent": intent, "threat_level": threat_level,
        }

    def _make_threat(self, level="MEDIUM", score=50, intent="unknown"):
        return {"threat_level": level, "score": score, "intent": intent}

    def test_returns_none_without_position(self):
        track = {"lat": None, "lon": None}
        result = roe.evaluate_track("T1", track, None, {}, {}, set())
        assert result is None

    def test_kill_zone_weapons_free(self, sample_zones, sample_assets):
        """HIGH threat inside kill zone → WEAPONS_FREE."""
        # Point inside zone-kill: [41.025-41.030, 28.980-28.990]
        track = self._make_track(41.027, 28.985, threat_level="HIGH")
        threat = self._make_threat("HIGH", 85, "attack")
        result = roe.evaluate_track("T1", track, threat,
                                    sample_zones, sample_assets, set())
        assert result["engagement"] == "WEAPONS_FREE"
        assert result["urgency"] == "CRITICAL"

    def test_high_attack_close_asset_weapons_free(self, sample_zones, sample_assets):
        """HIGH + attack + close to asset → WEAPONS_FREE."""
        # Place track very close to asset-hq (41.015, 28.979)
        track = self._make_track(41.0152, 28.9792, intent="attack",
                                 threat_level="HIGH")
        threat = self._make_threat("HIGH", 90, "attack")
        result = roe.evaluate_track("T1", track, threat,
                                    sample_zones, sample_assets, set())
        assert result["engagement"] == "WEAPONS_FREE"

    def test_high_attack_weapons_tight(self, sample_zones, sample_assets):
        """HIGH + attack intent (far) → WEAPONS_TIGHT."""
        track = self._make_track(41.050, 29.050, intent="attack",
                                 threat_level="HIGH")
        threat = self._make_threat("HIGH", 80, "attack")
        result = roe.evaluate_track("T1", track, threat,
                                    sample_zones, sample_assets, set())
        assert result["engagement"] == "WEAPONS_TIGHT"

    def test_high_near_asset_weapons_tight(self, sample_zones, sample_assets):
        """HIGH + close to asset (no attack intent) → WEAPONS_TIGHT."""
        track = self._make_track(41.016, 28.980, intent="reconnaissance",
                                 threat_level="HIGH")
        threat = self._make_threat("HIGH", 75, "reconnaissance")
        result = roe.evaluate_track("T1", track, threat,
                                    sample_zones, sample_assets, set())
        assert result["engagement"] == "WEAPONS_TIGHT"

    def test_high_far_weapons_hold(self, sample_zones, sample_assets):
        """HIGH threat far from everything → WEAPONS_HOLD."""
        track = self._make_track(41.100, 29.100, threat_level="HIGH")
        threat = self._make_threat("HIGH", 70)
        result = roe.evaluate_track("T1", track, threat,
                                    sample_zones, sample_assets, set())
        assert result["engagement"] == "WEAPONS_HOLD"

    def test_medium_in_restricted_zone(self, sample_zones, sample_assets):
        """MEDIUM threat inside restricted zone → WEAPONS_HOLD."""
        # Inside zone-restricted: [41.010-41.020, 28.970-28.990]
        track = self._make_track(41.015, 28.980, threat_level="MEDIUM")
        threat = self._make_threat("MEDIUM", 50)
        result = roe.evaluate_track("T1", track, threat,
                                    sample_zones, sample_assets, set())
        assert result["engagement"] == "WEAPONS_HOLD"

    def test_medium_coordinated_escalates(self, sample_zones, sample_assets):
        """MEDIUM threat + coordinated attack → escalated engagement."""
        track = self._make_track(41.050, 29.050, threat_level="MEDIUM")
        threat = self._make_threat("MEDIUM", 50)
        result = roe.evaluate_track("T1", track, threat,
                                    sample_zones, sample_assets, {"T1"})
        # Rule 8 sets WEAPONS_TIGHT, then coordinated modifier +1 → WEAPONS_FREE
        assert result["engagement"] == "WEAPONS_FREE"

    def test_low_threat_track_only(self, sample_zones, sample_assets):
        """LOW threat → TRACK_ONLY."""
        track = self._make_track(41.050, 29.050, threat_level="LOW")
        threat = self._make_threat("LOW", 10)
        result = roe.evaluate_track("T1", track, threat,
                                    sample_zones, sample_assets, set())
        assert result["engagement"] == "TRACK_ONLY"

    def test_friendly_zone_hold_fire(self, sample_zones, sample_assets):
        """LOW threat inside friendly zone → HOLD_FIRE."""
        # Inside zone-friendly: [41.000-41.005, 28.960-28.970]
        track = self._make_track(41.002, 28.965, threat_level="LOW")
        threat = self._make_threat("LOW", 5)
        result = roe.evaluate_track("T1", track, threat,
                                    sample_zones, sample_assets, set())
        assert result["engagement"] == "HOLD_FIRE"

    def test_high_score_escalation(self, sample_zones, sample_assets):
        """Score >= 90 escalates TRACK_ONLY → WEAPONS_TIGHT."""
        track = self._make_track(41.050, 29.050, threat_level="MEDIUM")
        threat = self._make_threat("MEDIUM", 92)
        result = roe.evaluate_track("T1", track, threat,
                                    sample_zones, sample_assets, set())
        assert result["engagement"] == "WEAPONS_TIGHT"

    def test_advisory_has_required_fields(self, sample_zones, sample_assets):
        track = self._make_track(41.015, 28.980, threat_level="HIGH")
        threat = self._make_threat("HIGH", 80, "attack")
        result = roe.evaluate_track("T1", track, threat,
                                    sample_zones, sample_assets, set())
        required = ["track_id", "engagement", "engagement_level", "urgency",
                    "reasons", "message", "time"]
        for field in required:
            assert field in result


# ── Batch evaluation tests ───────────────────────────────────────────────

class TestEvaluateAll:
    def test_batch_returns_sorted_list(self, sample_tracks, sample_threats,
                                       sample_zones, sample_assets):
        advisories = roe.evaluate_all(
            sample_tracks, sample_threats, sample_zones, sample_assets, []
        )
        assert isinstance(advisories, list)
        # Should be sorted by engagement level descending
        if len(advisories) >= 2:
            assert advisories[0]["engagement_level"] >= advisories[1]["engagement_level"]

    def test_tracks_without_threat_skipped(self, sample_zones, sample_assets):
        tracks = {"T-X": {"lat": 41.0, "lon": 29.0}}
        advisories = roe.evaluate_all(tracks, {}, sample_zones, sample_assets, [])
        assert advisories == []
