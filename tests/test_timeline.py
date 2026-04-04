"""
tests/test_timeline.py — Tests for ai/timeline.py
"""
import pytest
from ai import timeline


@pytest.fixture(autouse=True)
def _reset():
    timeline.reset()
    yield
    timeline.reset()


# ── Recording tests ──────────────────────────────────────────────────────

class TestRecordThreat:
    def test_basic_record(self):
        timeline.record_threat("T1", 50, "MEDIUM", "unknown", ts=1000.0)
        tl = timeline.get_timeline("T1")
        assert len(tl) == 1
        assert tl[0]["score"] == 50
        assert tl[0]["level"] == "MEDIUM"

    def test_multiple_records(self):
        for i in range(5):
            timeline.record_threat("T1", 50 + i * 10, "MEDIUM", "unknown",
                                   ts=1000.0 + i)
        tl = timeline.get_timeline("T1")
        assert len(tl) == 5

    def test_dedup_within_half_second(self):
        """Records within 0.5s update in-place rather than appending."""
        timeline.record_threat("T1", 50, "MEDIUM", "unknown", ts=1000.0)
        timeline.record_threat("T1", 80, "HIGH", "attack", ts=1000.3)
        tl = timeline.get_timeline("T1")
        assert len(tl) == 1
        assert tl[0]["score"] == 80
        assert tl[0]["level"] == "HIGH"

    def test_max_history_per_track(self):
        for i in range(timeline.MAX_HISTORY_PER_TRACK + 50):
            timeline.record_threat("T1", i, "LOW", "unknown", ts=1000.0 + i)
        tl = timeline.get_timeline("T1")
        assert len(tl) == timeline.MAX_HISTORY_PER_TRACK


class TestRecordAnomaly:
    def test_attach_to_existing_entry(self):
        timeline.record_threat("T1", 50, "MEDIUM", "unknown", ts=1000.0)
        timeline.record_anomaly("T1", "SPEED_SPIKE", "HIGH", ts=1000.5)
        tl = timeline.get_timeline("T1")
        assert len(tl[0]["events"]) == 1
        assert tl[0]["events"][0]["type"] == "SPEED_SPIKE"

    def test_create_placeholder_if_no_threat(self):
        timeline.record_anomaly("T-NEW", "HEADING_REVERSAL", "MEDIUM", ts=1000.0)
        tl = timeline.get_timeline("T-NEW")
        assert len(tl) == 1
        assert tl[0]["score"] == 0
        assert len(tl[0]["events"]) == 1


# ── Query tests ──────────────────────────────────────────────────────────

class TestQuery:
    def test_get_timeline_empty(self):
        assert timeline.get_timeline("nonexistent") == []

    def test_get_all_timelines(self):
        timeline.record_threat("T1", 50, "MEDIUM", "unknown", ts=1000.0)
        timeline.record_threat("T2", 30, "LOW", "unknown", ts=1000.0)
        all_tl = timeline.get_all_timelines()
        assert "T1" in all_tl
        assert "T2" in all_tl

    def test_get_active_track_ids(self):
        timeline.record_threat("T1", 50, "MEDIUM", "unknown", ts=1000.0)
        timeline.record_threat("T2", 30, "LOW", "unknown", ts=1000.0)
        ids = timeline.get_active_track_ids()
        assert "T1" in ids
        assert "T2" in ids

    def test_get_summary(self):
        timeline.record_threat("T1", 50, "MEDIUM", "unknown", ts=1000.0)
        timeline.record_threat("T1", 60, "MEDIUM", "unknown", ts=1001.0)
        summary = timeline.get_summary()
        assert summary["tracked_count"] == 1
        assert summary["total_points"] == 2


# ── LRU eviction tests ──────────────────────────────────────────────────

class TestLRUEviction:
    def test_evicts_oldest_when_over_max(self):
        # Fill to MAX_TRACKS + 1
        for i in range(timeline.MAX_TRACKS + 1):
            timeline.record_threat(f"T-{i:04d}", 50, "LOW", "unknown",
                                   ts=1000.0 + i)
        # First track should have been evicted
        assert timeline.get_timeline("T-0000") == []
        # Last track should exist
        assert len(timeline.get_timeline(f"T-{timeline.MAX_TRACKS:04d}")) == 1


# ── Lifecycle tests ──────────────────────────────────────────────────────

class TestLifecycle:
    def test_remove_track(self):
        timeline.record_threat("T1", 50, "MEDIUM", "unknown", ts=1000.0)
        timeline.remove_track("T1")
        assert timeline.get_timeline("T1") == []

    def test_reset(self):
        timeline.record_threat("T1", 50, "MEDIUM", "unknown", ts=1000.0)
        timeline.record_threat("T2", 50, "MEDIUM", "unknown", ts=1000.0)
        timeline.reset()
        assert timeline.get_all_timelines() == {}
