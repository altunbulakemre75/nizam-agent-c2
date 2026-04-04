"""
tests/test_anomaly.py — Tests for ai/anomaly.py
"""
import math
import pytest
from ai import anomaly


@pytest.fixture(autouse=True)
def _reset():
    anomaly.reset()
    yield
    anomaly.reset()


# ── Helper tests ──────────────────────────────────────────────────────────

class TestHaversine:
    def test_same_point(self):
        assert anomaly._haversine_m(41.0, 29.0, 41.0, 29.0) == 0.0

    def test_known_distance(self):
        # ~111km per degree latitude
        d = anomaly._haversine_m(41.0, 29.0, 42.0, 29.0)
        assert abs(d - 111_320) < 100

    def test_symmetric(self):
        d1 = anomaly._haversine_m(41.0, 29.0, 41.01, 29.01)
        d2 = anomaly._haversine_m(41.01, 29.01, 41.0, 29.0)
        assert abs(d1 - d2) < 1.0


class TestBearing:
    def test_north(self):
        b = anomaly._bearing_deg(41.0, 29.0, 42.0, 29.0)
        assert abs(b - 0.0) < 1.0

    def test_east(self):
        b = anomaly._bearing_deg(41.0, 29.0, 41.0, 30.0)
        assert abs(b - 90.0) < 1.0

    def test_south(self):
        b = anomaly._bearing_deg(42.0, 29.0, 41.0, 29.0)
        assert abs(b - 180.0) < 1.0


class TestAngleDiff:
    def test_zero(self):
        assert anomaly._angle_diff(90, 90) == 0.0

    def test_normal(self):
        assert abs(anomaly._angle_diff(10, 350) - 20) < 0.01

    def test_opposite(self):
        assert abs(anomaly._angle_diff(0, 180) - 180) < 0.01

    def test_wraparound(self):
        assert abs(anomaly._angle_diff(5, 355) - 10) < 0.01


# ── Track anomaly tests ──────────────────────────────────────────────────

class TestCheckTrack:
    def test_first_update_no_anomaly(self):
        result = anomaly.check_track("T1", 41.0, 29.0, ts=1000.0)
        assert result == []

    def test_needs_baseline_updates(self):
        """No anomalies until MIN_UPDATES_FOR_ANOMALY updates."""
        for i in range(anomaly.MIN_UPDATES_FOR_ANOMALY):
            lat = 41.0 + i * 0.0001
            result = anomaly.check_track("T1", lat, 29.0, ts=1000.0 + i)
        # All should return no anomalies during baseline
        assert result == []

    def test_speed_spike_detected(self, offset_m):
        """Feed constant speed then a sudden jump."""
        base_lat, base_lon = 41.0, 29.0
        # Build baseline: constant ~10 m/s northward
        for i in range(5):
            lat = base_lat + i * (10.0 / 111320.0)
            anomaly.check_track("T1", lat, base_lon, ts=1000.0 + i)

        # Sudden jump: ~200 m/s (big spike)
        jump_lat = base_lat + 5 * (10.0 / 111320.0) + (200.0 / 111320.0)
        result = anomaly.check_track("T1", jump_lat, base_lon, ts=1006.0)
        types = [a["type"] for a in result]
        assert "SPEED_SPIKE" in types

    def test_heading_reversal_detected(self, offset_m):
        """Moving north then suddenly south = heading reversal."""
        base_lat, base_lon = 41.0, 29.0
        # Move north at ~50 m/s
        for i in range(5):
            lat = base_lat + i * (50.0 / 111320.0)
            anomaly.check_track("T1", lat, base_lon, ts=1000.0 + i)

        # Now move south (reversal)
        prev_lat = base_lat + 4 * (50.0 / 111320.0)
        reverse_lat = prev_lat - (50.0 / 111320.0)
        result = anomaly.check_track("T1", reverse_lat, base_lon, ts=1006.0)
        types = [a["type"] for a in result]
        assert "HEADING_REVERSAL" in types

    def test_intent_shift_detected(self):
        """Shift from loitering to attack triggers INTENT_SHIFT."""
        for i in range(5):
            lat = 41.0 + i * 0.0001
            anomaly.check_track("T1", lat, 29.0, intent="loitering", ts=1000.0 + i)

        result = anomaly.check_track("T1", 41.001, 29.0, intent="attack", ts=1006.0)
        types = [a["type"] for a in result]
        assert "INTENT_SHIFT" in types

    def test_no_intent_shift_for_same_intent(self):
        """No anomaly when intent stays the same."""
        for i in range(5):
            anomaly.check_track("T1", 41.0 + i * 0.0001, 29.0,
                                intent="attack", ts=1000.0 + i)
        result = anomaly.check_track("T1", 41.001, 29.0,
                                     intent="attack", ts=1006.0)
        types = [a["type"] for a in result]
        assert "INTENT_SHIFT" not in types


# ── Swarm detection tests ────────────────────────────────────────────────

class TestDetectSwarms:
    def _setup_cluster(self, n, base_lat=41.0, base_lon=29.0, heading_deg=0):
        """Create n tracks close together with similar headings."""
        tracks = {}
        for i in range(n):
            tid = f"S-{i:03d}"
            lat = base_lat + i * 0.001  # ~111m apart
            lon = base_lon
            # Need to register each track in _stats
            anomaly.check_track(tid, lat - 0.001, lon, ts=1000.0)
            anomaly.check_track(tid, lat, lon, ts=1001.0)
            tracks[tid] = {"lat": lat, "lon": lon}
        return tracks

    def test_no_swarm_too_few_tracks(self):
        tracks = self._setup_cluster(2)
        result = anomaly.detect_swarms(tracks)
        assert result == []

    def test_swarm_detected(self):
        tracks = self._setup_cluster(4)
        result = anomaly.detect_swarms(tracks)
        assert len(result) >= 1
        assert result[0]["type"] == "SWARM_DETECTED"
        assert result[0]["severity"] == "CRITICAL"

    def test_no_swarm_tracks_too_far(self):
        tracks = {}
        for i in range(4):
            tid = f"F-{i:03d}"
            lat = 41.0 + i * 0.1  # ~11km apart — too far
            anomaly.check_track(tid, lat - 0.001, 29.0, ts=1000.0)
            anomaly.check_track(tid, lat, 29.0, ts=1001.0)
            tracks[tid] = {"lat": lat, "lon": 29.0}
        result = anomaly.detect_swarms(tracks)
        assert result == []


# ── Lifecycle tests ──────────────────────────────────────────────────────

class TestLifecycle:
    def test_remove_track(self):
        anomaly.check_track("T1", 41.0, 29.0, ts=1000.0)
        assert "T1" in anomaly._stats
        anomaly.remove_track("T1")
        assert "T1" not in anomaly._stats

    def test_reset_clears_all(self):
        anomaly.check_track("T1", 41.0, 29.0, ts=1000.0)
        anomaly.check_track("T2", 42.0, 29.0, ts=1000.0)
        anomaly.reset()
        assert len(anomaly._stats) == 0
