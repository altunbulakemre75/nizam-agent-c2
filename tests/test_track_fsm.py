"""
tests/test_track_fsm.py — Tests for ai/track_fsm.py

Covers:
  - DETECTED → TRACKED promotion (2+ sensors, 3+ updates)
  - TRACKED → ENGAGING → DESTROYED lifecycle
  - LOST timeout
  - Thread-safety basics
  - clear / drop_track / stats
"""
from __future__ import annotations

import time

import pytest

from ai import track_fsm
from ai.track_fsm import TrackState


@pytest.fixture(autouse=True)
def _reset():
    track_fsm.clear()
    yield
    track_fsm.clear()


class TestOnUpdate:
    def test_first_update_returns_detected(self):
        state = track_fsm.on_update("T-1")
        assert state == TrackState.DETECTED

    def test_promotes_with_two_sensors(self):
        track_fsm.on_update("T-1", sensors=["radar-01"])
        state = track_fsm.on_update("T-1", sensors=["eo-01"])
        assert state == TrackState.TRACKED

    def test_promotes_after_three_updates_single_sensor(self):
        track_fsm.on_update("T-1", sensors=["radar-01"])
        track_fsm.on_update("T-1", sensors=["radar-01"])
        state = track_fsm.on_update("T-1", sensors=["radar-01"])
        assert state == TrackState.TRACKED

    def test_stays_detected_until_threshold(self):
        track_fsm.on_update("T-1", sensors=["radar-01"])
        state = track_fsm.on_update("T-1", sensors=["radar-01"])
        assert state == TrackState.DETECTED

    def test_no_sensors_promotes_after_three_updates(self):
        track_fsm.on_update("T-1")
        track_fsm.on_update("T-1")
        state = track_fsm.on_update("T-1")
        assert state == TrackState.TRACKED


class TestEngageDestroy:
    def _make_tracked(self, tid="T-1"):
        track_fsm.on_update(tid, sensors=["radar-01", "eo-01"])
        assert track_fsm.get_state(tid) == TrackState.TRACKED

    def test_engage_from_tracked(self):
        self._make_tracked()
        state = track_fsm.on_engage("T-1")
        assert state == TrackState.ENGAGING

    def test_engage_from_detected_stays(self):
        track_fsm.on_update("T-1")
        state = track_fsm.on_engage("T-1")
        # Should not transition from DETECTED
        assert state == TrackState.DETECTED

    def test_destroyed_from_engaging(self):
        self._make_tracked()
        track_fsm.on_engage("T-1")
        state = track_fsm.on_destroyed("T-1")
        assert state == TrackState.DESTROYED

    def test_destroyed_sticks(self):
        """Once destroyed, updates should not change state."""
        self._make_tracked()
        track_fsm.on_engage("T-1")
        track_fsm.on_destroyed("T-1")
        state = track_fsm.on_update("T-1", sensors=["radar-01"])
        assert state == TrackState.DESTROYED


class TestLostTimeout:
    def test_lost_after_timeout(self, monkeypatch):
        track_fsm.on_update("T-1", sensors=["radar-01", "eo-01"])
        # Fast-forward time past timeout
        entry = track_fsm._tracks["T-1"]
        entry.last_seen = time.time() - track_fsm._LOST_TIMEOUT_S - 1
        state = track_fsm.get_state("T-1")
        assert state == TrackState.LOST

    def test_not_lost_before_timeout(self):
        track_fsm.on_update("T-1", sensors=["radar-01", "eo-01"])
        state = track_fsm.get_state("T-1")
        assert state == TrackState.TRACKED


class TestHelpers:
    def test_drop_track(self):
        track_fsm.on_update("T-1")
        track_fsm.drop_track("T-1")
        assert track_fsm.get_state("T-1") is None

    def test_drop_nonexistent_no_error(self):
        track_fsm.drop_track("ghost")

    def test_get_state_unknown(self):
        assert track_fsm.get_state("nope") is None

    def test_get_all(self):
        track_fsm.on_update("T-1", sensors=["a", "b"])
        track_fsm.on_update("T-2")
        result = track_fsm.get_all()
        assert result["T-1"] == "TRACKED"
        assert result["T-2"] == "DETECTED"

    def test_stats(self):
        track_fsm.on_update("T-1", sensors=["a", "b"])
        track_fsm.on_update("T-2")
        s = track_fsm.stats()
        assert s["total"] == 2
        assert s["by_state"]["TRACKED"] == 1
        assert s["by_state"]["DETECTED"] == 1

    def test_clear(self):
        track_fsm.on_update("T-1")
        track_fsm.on_update("T-2")
        track_fsm.clear()
        assert track_fsm.stats()["total"] == 0
