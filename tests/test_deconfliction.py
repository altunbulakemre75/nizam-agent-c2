"""
tests/test_deconfliction.py — Tests for ai/deconfliction.py

Covers:
  - Similarity scoring: position, heading, speed components
  - Hard position gate (> 200m = no match)
  - find_match: exact duplicate, near-duplicate, distinct track
  - Friendly/hostile cross-match prevention
  - record_merge, resolve, get_aliases
  - merge_sensors deduplication
  - reset
"""
from __future__ import annotations

import pytest
from ai import deconfliction as dc


@pytest.fixture(autouse=True)
def _reset():
    dc.reset()
    yield
    dc.reset()


# ── Similarity helpers ────────────────────────────────────────────────────

class TestSimilarity:
    def _track(self, lat, lon, heading=0.0, speed=30.0):
        return {"lat": lat, "lon": lon, "heading": heading, "speed": speed}

    def test_identical_tracks_score_one(self):
        t = self._track(41.0, 29.0, heading=180.0, speed=30.0)
        score = dc._similarity(t, t)
        assert score >= 0.99

    def test_beyond_pos_gate_returns_zero(self):
        t1 = self._track(41.0, 29.0)
        # ~220 m north — beyond 200 m gate
        t2 = self._track(41.002, 29.0)
        assert dc._similarity(t1, t2) == 0.0

    def test_within_gate_but_different_heading(self):
        t1 = self._track(41.0, 29.0, heading=0.0)
        t2 = self._track(41.0005, 29.0, heading=180.0)  # ~55m, opposite heading
        score = dc._similarity(t1, t2)
        # Position component is high, heading kills the score
        assert 0.0 < score < dc.MATCH_THRESHOLD

    def test_missing_position_returns_zero(self):
        t1 = {"speed": 30.0}
        t2 = {"lat": 41.0, "lon": 29.0}
        assert dc._similarity(t1, t2) == 0.0

    def test_kinematics_fallback(self):
        """Speed/heading from nested kinematics dict should also work."""
        t1 = {"lat": 41.0, "lon": 29.0,
              "kinematics": {"speed_mps": 25.0, "heading_deg": 90.0}}
        t2 = {"lat": 41.0002, "lon": 29.0,  # ~22m apart
              "kinematics": {"speed_mps": 25.0, "heading_deg": 90.0}}
        score = dc._similarity(t1, t2)
        assert score >= dc.MATCH_THRESHOLD


# ── find_match ────────────────────────────────────────────────────────────

class TestFindMatch:
    def _make_existing(self, lat, lon, heading=180.0, speed=35.0):
        return {
            "lat": lat, "lon": lon,
            "heading": heading, "speed": speed,
            "classification": {"label": "drone"},
        }

    def test_no_match_empty_state(self):
        t = {"lat": 41.0, "lon": 29.0, "heading": 180.0, "speed": 35.0}
        assert dc.find_match(t, {}) is None

    def test_near_duplicate_matched(self):
        existing = {"T-001": self._make_existing(41.0, 29.0, heading=180.0, speed=35.0)}
        # 50m away, same heading+speed
        new_t = {"lat": 41.00045, "lon": 29.0, "heading": 180.0, "speed": 35.0,
                 "classification": {"label": "drone"}}
        result = dc.find_match(new_t, existing)
        assert result is not None
        assert result[0] == "T-001"
        assert result[1] >= dc.MATCH_THRESHOLD

    def test_distinct_track_no_match(self):
        existing = {"T-001": self._make_existing(41.0, 29.0)}
        # 500m away — beyond gate
        new_t = {"lat": 41.005, "lon": 29.0, "heading": 180.0, "speed": 35.0,
                 "classification": {"label": "drone"}}
        assert dc.find_match(new_t, existing) is None

    def test_best_match_returned(self):
        """If two candidates exist, the closer one wins."""
        existing = {
            "T-001": self._make_existing(41.001, 29.0),   # ~111m
            "T-002": self._make_existing(41.0005, 29.0),  # ~55m — closer
        }
        new_t = {"lat": 41.0, "lon": 29.0, "heading": 180.0, "speed": 35.0,
                 "classification": {"label": "drone"}}
        result = dc.find_match(new_t, existing)
        assert result is not None
        assert result[0] == "T-002"


# ── Cross-type match prevention ───────────────────────────────────────────

class TestFriendlyHostileSeparation:
    def test_friendly_never_merged_with_hostile(self):
        existing = {
            "A-001": {
                "lat": 41.0, "lon": 29.0,
                "heading": 180.0, "speed": 35.0,
                "type": "friendly",
            }
        }
        new_hostile = {
            "lat": 41.0001, "lon": 29.0,
            "heading": 180.0, "speed": 35.0,
            "classification": {"label": "drone"},
        }
        assert dc.find_match(new_hostile, existing) is None

    def test_friendly_matches_friendly(self):
        existing = {
            "A-001": {
                "lat": 41.0, "lon": 29.0,
                "heading": 180.0, "speed": 35.0,
                "type": "friendly",
            }
        }
        new_friendly = {
            "lat": 41.0001, "lon": 29.0,
            "heading": 180.0, "speed": 35.0,
            "type": "friendly",
        }
        result = dc.find_match(new_friendly, existing)
        assert result is not None
        assert result[0] == "A-001"


# ── Alias / merge bookkeeping ─────────────────────────────────────────────

class TestAliasBookkeeping:
    def test_record_and_resolve(self):
        dc.record_merge("ADSB-001", "T-001")
        assert dc.resolve("ADSB-001") == "T-001"
        assert dc.resolve("T-001") == "T-001"

    def test_get_aliases(self):
        dc.record_merge("ADSB-001", "T-001")
        dc.record_merge("MQTT-007", "T-001")
        aliases = dc.get_aliases("T-001")
        assert "ADSB-001" in aliases
        assert "MQTT-007" in aliases

    def test_idempotent_record(self):
        dc.record_merge("A", "T-001")
        dc.record_merge("A", "T-001")
        assert dc.get_aliases("T-001").count("A") == 1

    def test_stats(self):
        dc.record_merge("X-1", "T-001")
        dc.record_merge("X-2", "T-001")
        s = dc.stats()
        assert s["total_aliases"] == 2
        assert s["canonical_count"] == 1

    def test_reset_clears_all(self):
        dc.record_merge("X-1", "T-001")
        dc.reset()
        assert dc.stats()["total_aliases"] == 0
        assert dc.resolve("X-1") == "X-1"


# ── merge_sensors ─────────────────────────────────────────────────────────

class TestMergeSensors:
    def test_merge_deduplicates(self):
        canonical = {"supporting_sensors": ["radar-01", "eo-01"]}
        duplicate = {"supporting_sensors": ["eo-01", "mqtt-02"]}
        result = dc.merge_sensors(canonical, duplicate)
        assert sorted(result) == ["eo-01", "mqtt-02", "radar-01"]

    def test_merge_empty_lists(self):
        assert dc.merge_sensors({}, {}) == []

    def test_merge_one_empty(self):
        canonical = {"supporting_sensors": ["radar-01"]}
        result = dc.merge_sensors(canonical, {})
        assert result == ["radar-01"]
