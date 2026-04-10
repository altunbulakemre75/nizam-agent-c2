"""
tests/test_ew_detector.py — Tests for ai/ew_detector.py

Covers:
  - GPS spoofing: impossible position jump detected
  - GPS spoofing: normal movement not flagged
  - False injection: single sensor flooding new tracks
  - False injection: new tracks from different sensors not flagged
  - Mass jamming: simultaneous stale tracks flagged
  - Mass jamming: debounce (no double-fire within 30s)
  - Mass jamming: not triggered with few stale tracks
  - stats, reset, remove_track
"""
from __future__ import annotations

import time
import pytest
from ai import ew_detector as ew


@pytest.fixture(autouse=True)
def _reset():
    ew.reset()
    yield
    ew.reset()


# ── GPS Spoofing ──────────────────────────────────────────────────────────

class TestGPSSpoofing:
    def test_normal_movement_no_alert(self):
        # ~35 m/s northward movement — normal drone speed
        ew.on_track_update("T-1", 41.0000, 29.0, ts=1000.0)
        alerts = ew.on_track_update("T-1", 41.0003, 29.0, ts=1001.0)
        types = [a["type"] for a in alerts]
        assert "GPS_SPOOFING" not in types

    def test_impossible_jump_flagged(self):
        # First update
        ew.on_track_update("T-1", 41.0, 29.0, ts=1000.0)
        # Jump ~50km in 1 second → ~50,000 m/s >> 500 m/s gate
        alerts = ew.on_track_update("T-1", 41.5, 29.0, ts=1001.0)
        types = [a["type"] for a in alerts]
        assert "GPS_SPOOFING" in types

    def test_severity_is_critical(self):
        ew.on_track_update("T-1", 41.0, 29.0, ts=1000.0)
        alerts = ew.on_track_update("T-1", 41.5, 29.0, ts=1001.0)
        spoof = next(a for a in alerts if a["type"] == "GPS_SPOOFING")
        assert spoof["severity"] == "CRITICAL"
        assert spoof["track_id"] == "T-1"

    def test_first_update_no_alert(self):
        # No previous state — can't compute jump
        alerts = ew.on_track_update("T-NEW", 41.5, 29.0, ts=1001.0)
        assert alerts == []

    def test_borderline_speed_not_flagged(self):
        # ~490 m/s — just under the gate
        ew.on_track_update("T-1", 41.0, 29.0, ts=1000.0)
        # 490m in 1s
        alerts = ew.on_track_update("T-1", 41.0044, 29.0, ts=1001.0)
        types = [a["type"] for a in alerts]
        assert "GPS_SPOOFING" not in types


# ── False Injection ───────────────────────────────────────────────────────

class TestFalseInjection:
    def test_burst_of_new_tracks_flagged(self):
        sensor = "mqtt-attacker"
        alerts_seen = []
        for i in range(ew.INJECTION_RATE_THRESH + 2):
            tid = f"FAKE-{i:03d}"
            alerts = ew.on_track_update(
                tid, 41.0 + i * 0.001, 29.0,
                sensors=[sensor],
                ts=1000.0 + i * 0.5,
            )
            alerts_seen.extend(alerts)
        types = [a["type"] for a in alerts_seen]
        assert "FALSE_INJECTION" in types

    def test_normal_rate_not_flagged(self):
        sensor = "radar-01"
        alerts_seen = []
        # Send only INJECTION_RATE_THRESH tracks — exactly at threshold, not over
        for i in range(ew.INJECTION_RATE_THRESH):
            alerts = ew.on_track_update(
                f"T-{i:03d}", 41.0 + i * 0.001, 29.0,
                sensors=[sensor],
                ts=1000.0 + i,
            )
            alerts_seen.extend(alerts)
        types = [a["type"] for a in alerts_seen]
        assert "FALSE_INJECTION" not in types

    def test_different_sensors_not_flagged(self):
        """Same number of tracks but spread across different sensors → no alert."""
        alerts_seen = []
        for i in range(ew.INJECTION_RATE_THRESH + 2):
            alerts = ew.on_track_update(
                f"T-{i:03d}", 41.0 + i * 0.001, 29.0,
                sensors=[f"sensor-{i}"],
                ts=1000.0 + i * 0.5,
            )
            alerts_seen.extend(alerts)
        types = [a["type"] for a in alerts_seen]
        assert "FALSE_INJECTION" not in types

    def test_window_expires(self):
        """Tracks spread outside the injection window should not trigger."""
        sensor = "mqtt-slow"
        alerts_seen = []
        for i in range(ew.INJECTION_RATE_THRESH + 2):
            alerts = ew.on_track_update(
                f"T-W-{i:03d}", 41.0 + i * 0.001, 29.0,
                sensors=[sensor],
                # Space events beyond the window
                ts=1000.0 + i * (ew.INJECTION_WINDOW_S + 1),
            )
            alerts_seen.extend(alerts)
        types = [a["type"] for a in alerts_seen]
        assert "FALSE_INJECTION" not in types


# ── Mass Jamming ──────────────────────────────────────────────────────────

class TestMassJamming:
    def _register_stale_tracks(self, n, base_ts=None):
        """Register n tracks with old timestamps to simulate jamming."""
        old_ts = (base_ts or time.time()) - ew.JAMMING_STALE_S - 2.0
        tracks = {}
        for i in range(n):
            tid = f"J-{i:03d}"
            ew.on_track_update(tid, 41.0 + i * 0.001, 29.0, ts=old_ts + i * 0.1)
            tracks[tid] = {"lat": 41.0 + i * 0.001, "lon": 29.0}
        return tracks

    def test_mass_stale_triggers_jamming(self):
        tracks = self._register_stale_tracks(ew.JAMMING_TRACK_COUNT + 1)
        alerts = ew.check_mass_jamming(tracks)
        types = [a["type"] for a in alerts]
        assert "RADAR_JAMMING" in types

    def test_few_stale_tracks_no_alert(self):
        tracks = self._register_stale_tracks(ew.JAMMING_TRACK_COUNT - 1)
        alerts = ew.check_mass_jamming(tracks)
        assert alerts == []

    def test_jamming_debounced(self):
        """Second call within 30s should not re-fire."""
        tracks = self._register_stale_tracks(ew.JAMMING_TRACK_COUNT + 1)
        first = ew.check_mass_jamming(tracks)
        assert len(first) == 1
        second = ew.check_mass_jamming(tracks)
        assert second == []


# ── Lifecycle ─────────────────────────────────────────────────────────────

class TestLifecycle:
    def test_remove_track(self):
        ew.on_track_update("T-1", 41.0, 29.0, ts=1000.0)
        assert "T-1" in ew._tracks
        ew.remove_track("T-1")
        assert "T-1" not in ew._tracks

    def test_reset_clears_all(self):
        ew.on_track_update("T-1", 41.0, 29.0, ts=1000.0, sensors=["s1"])
        ew.reset()
        assert ew.stats()["tracked_count"] == 0
        assert ew.stats()["sensor_windows"] == {}

    def test_stats(self):
        ew.on_track_update("T-1", 41.0, 29.0, ts=1000.0)
        ew.on_track_update("T-2", 41.001, 29.0, ts=1000.0)
        assert ew.stats()["tracked_count"] == 2
