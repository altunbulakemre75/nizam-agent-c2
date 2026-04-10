"""
tests/test_ew_ml.py — Unit tests for ai/ew_ml.py

Covers all four detectors:
  - SpeedZScoreDetector: gradual spoof via speed outlier
  - TrajectoryDeviationDetector: dead-reckoning deviation
  - CoordinatedSpoofDetector: N tracks jump same vector
  - JammingSweepDetector: PCA corridor detection
"""
from __future__ import annotations

import math
import time
import pytest
from ai import ew_ml as ew


@pytest.fixture(autouse=True)
def _reset():
    ew.reset()
    yield
    ew.reset()


# ── SpeedZScoreDetector ───────────────────────────────────────────────────────

class TestSpeedZScore:
    def _build_history(self, track_id: str, speed_ms: float, n: int,
                       base_ts: float = 1000.0) -> None:
        """Inject n steady updates to build speed history."""
        lat, lon = 41.0, 29.0
        step = speed_ms / ew.DEG_TO_M  # roughly 1 second of movement
        for i in range(n):
            ew.on_track_update(track_id, lat + i * step, lon,
                               speed_ms=speed_ms,
                               ts=base_ts + i)

    def test_no_alert_with_insufficient_history(self):
        # Only 2 updates — below SPEED_MIN_SAMPLES
        ew.on_track_update("T-1", 41.0000, 29.0, speed_ms=20.0, ts=1000.0)
        alerts = ew.on_track_update("T-1", 41.0002, 29.0, speed_ms=500.0, ts=1001.0)
        types = [a["type"] for a in alerts]
        assert "GPS_SPOOFING_GRADUAL" not in types

    def test_alert_on_zscore_outlier(self):
        # Build history at ~20 m/s, then spike to 200 m/s
        self._build_history("T-1", 20.0, ew.SPEED_MIN_SAMPLES + 3)
        # Now spike the speed: move ~200m in 1s
        last_lat = 41.0 + (ew.SPEED_MIN_SAMPLES + 3) * (20.0 / ew.DEG_TO_M)
        alerts = ew.on_track_update(
            "T-1",
            last_lat + 200.0 / ew.DEG_TO_M,  # 200m step in 1 second
            29.0,
            speed_ms=200.0,
            ts=1000.0 + ew.SPEED_MIN_SAMPLES + 4,
        )
        types = [a["type"] for a in alerts]
        assert "GPS_SPOOFING_GRADUAL" in types

    def test_no_alert_on_normal_speed(self):
        self._build_history("T-1", 20.0, ew.SPEED_MIN_SAMPLES + 3)
        last_lat = 41.0 + (ew.SPEED_MIN_SAMPLES + 3) * (20.0 / ew.DEG_TO_M)
        # Move at roughly the same speed
        alerts = ew.on_track_update(
            "T-1",
            last_lat + 22.0 / ew.DEG_TO_M,
            29.0,
            speed_ms=22.0,
            ts=1000.0 + ew.SPEED_MIN_SAMPLES + 4,
        )
        types = [a["type"] for a in alerts]
        assert "GPS_SPOOFING_GRADUAL" not in types


# ── TrajectoryDeviationDetector ───────────────────────────────────────────────

class TestTrajectoryDeviation:
    def test_no_alert_on_straight_flight(self):
        # Heading north, 30 m/s
        ew.on_track_update("T-2", 41.0, 29.0, speed_ms=30.0, heading=0.0, ts=1000.0)
        # In 10s at 30 m/s north: ~270m = 0.0024°
        alerts = ew.on_track_update(
            "T-2", 41.0024, 29.0,
            speed_ms=30.0, heading=0.0, ts=1010.0,
        )
        types = [a["type"] for a in alerts]
        assert "TRAJECTORY_DEVIATION" not in types

    def test_alert_on_large_deviation(self):
        # Heading north at 30 m/s — but position actually jumps east 1km
        ew.on_track_update("T-2", 41.0, 29.0, speed_ms=30.0, heading=0.0, ts=1000.0)
        # Jump 1 km east in 10 seconds — dead-reckoning says ~270m north
        east_jump = 1000.0 / (ew.DEG_TO_M * math.cos(math.radians(41.0)))
        alerts = ew.on_track_update(
            "T-2", 41.0, 29.0 + east_jump,
            speed_ms=30.0, heading=0.0, ts=1010.0,
        )
        types = [a["type"] for a in alerts]
        assert "TRAJECTORY_DEVIATION" in types

    def test_stationary_track_no_alert(self):
        # Speed=0 — dead-reckoning not applicable
        ew.on_track_update("T-3", 41.0, 29.0, speed_ms=0.0, heading=0.0, ts=1000.0)
        alerts = ew.on_track_update(
            "T-3", 41.001, 29.001,
            speed_ms=0.0, heading=0.0, ts=1010.0,
        )
        types = [a["type"] for a in alerts]
        assert "TRAJECTORY_DEVIATION" not in types

    def test_long_dt_skipped(self):
        # dt > DEVIATION_MAX_DT_S — skip the check
        ew.on_track_update("T-4", 41.0, 29.0, speed_ms=30.0, heading=0.0, ts=1000.0)
        alerts = ew.on_track_update(
            "T-4", 41.5, 29.5,
            speed_ms=30.0, heading=0.0,
            ts=1000.0 + ew.DEVIATION_MAX_DT_S + 10,
        )
        types = [a["type"] for a in alerts]
        assert "TRAJECTORY_DEVIATION" not in types


# ── CoordinatedSpoofDetector ──────────────────────────────────────────────────

class TestCoordinatedSpoof:
    def _jump(self, track_id: str, from_lat: float, from_lon: float,
              to_lat: float, to_lon: float, ts: float) -> list:
        ew.on_track_update(track_id, from_lat, from_lon, ts=ts - 1)
        return ew.on_track_update(track_id, to_lat, to_lon, ts=ts)

    def test_correlated_jumps_flagged(self):
        # 3 tracks all jump ~500m north within 2 seconds
        step = 500.0 / ew.DEG_TO_M
        all_alerts = []
        for i in range(ew.CORR_MIN_TRACKS):
            alerts = self._jump(
                f"T-C{i}",
                41.0 + i * 0.001, 29.0,
                41.0 + i * 0.001 + step, 29.0,
                ts=1000.0 + i * 0.5,
            )
            all_alerts.extend(alerts)
        types = [a["type"] for a in all_alerts]
        assert "COORDINATED_SPOOF" in types

    def test_independent_jumps_not_flagged(self):
        # 3 tracks jump in completely different directions
        step = 500.0 / ew.DEG_TO_M
        all_alerts = []
        offsets = [(step, 0), (-step, 0), (0, step)]
        for i, (dlat, dlon) in enumerate(offsets):
            alerts = self._jump(
                f"T-D{i}",
                41.0 + i * 0.001, 29.0,
                41.0 + i * 0.001 + dlat, 29.0 + dlon,
                ts=1000.0 + i * 0.1,
            )
            all_alerts.extend(alerts)
        types = [a["type"] for a in all_alerts]
        assert "COORDINATED_SPOOF" not in types

    def test_jumps_outside_window_not_flagged(self):
        # Same direction but spread > CORR_WINDOW_S apart
        step = 500.0 / ew.DEG_TO_M
        all_alerts = []
        for i in range(ew.CORR_MIN_TRACKS):
            alerts = self._jump(
                f"T-E{i}",
                41.0 + i * 0.001, 29.0,
                41.0 + i * 0.001 + step, 29.0,
                ts=1000.0 + i * (ew.CORR_WINDOW_S + 2),
            )
            all_alerts.extend(alerts)
        types = [a["type"] for a in all_alerts]
        assert "COORDINATED_SPOOF" not in types


# ── JammingSweepDetector ──────────────────────────────────────────────────────

class TestJammingSweep:
    def _register_stale_linear(self, n: int, base_ts: float = None) -> None:
        """Register n tracks with old timestamps arranged in a line (north-south)."""
        old_ts = (base_ts or time.time()) - ew.SWEEP_STALE_S - 2.0
        for i in range(n):
            tid = f"SWEEP-{i:03d}"
            ew.on_track_update(tid, 41.0 + i * 0.01, 29.0, ts=old_ts + i * 0.1)

    def _register_stale_clustered(self, n: int) -> None:
        """Register n tracks with old timestamps in a tight cluster (not a line)."""
        old_ts = time.time() - ew.SWEEP_STALE_S - 2.0
        for i in range(n):
            tid = f"CLUST-{i:03d}"
            ew.on_track_update(
                tid, 41.0 + (i % 3) * 0.001, 29.0 + (i // 3) * 0.001,
                ts=old_ts + i * 0.1,
            )

    def test_linear_pattern_flagged(self):
        self._register_stale_linear(ew.SWEEP_MIN_TRACKS + 2)
        alerts = ew.check_patterns({})
        types = [a["type"] for a in alerts]
        assert "JAMMING_SWEEP" in types

    def test_clustered_pattern_not_flagged(self):
        self._register_stale_clustered(ew.SWEEP_MIN_TRACKS + 2)
        alerts = ew.check_patterns({})
        types = [a["type"] for a in alerts]
        assert "JAMMING_SWEEP" not in types

    def test_too_few_stale_not_flagged(self):
        self._register_stale_linear(ew.SWEEP_MIN_TRACKS - 1)
        alerts = ew.check_patterns({})
        assert alerts == []

    def test_sweep_debounced(self):
        self._register_stale_linear(ew.SWEEP_MIN_TRACKS + 2)
        first = ew.check_patterns({})
        assert any(a["type"] == "JAMMING_SWEEP" for a in first)
        second = ew.check_patterns({})
        types = [a["type"] for a in second]
        assert "JAMMING_SWEEP" not in types


# ── stats / reset ─────────────────────────────────────────────────────────────

class TestStats:
    def test_stats_initial(self):
        s = ew.stats()
        assert s["tracked_count"] == 0
        assert s["jump_events"] == 0
        assert "config" in s

    def test_stats_after_updates(self):
        ew.on_track_update("T-1", 41.0, 29.0, ts=1000.0)
        ew.on_track_update("T-2", 41.1, 29.0, ts=1001.0)
        assert ew.stats()["tracked_count"] == 2

    def test_reset_clears_all(self):
        ew.on_track_update("T-1", 41.0, 29.0, ts=1000.0)
        ew.reset()
        assert ew.stats()["tracked_count"] == 0
        assert ew.stats()["jump_events"] == 0
