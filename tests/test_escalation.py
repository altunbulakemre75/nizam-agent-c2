"""
tests/test_escalation.py — Unit tests for ai/escalation.py

Covers:
  - No escalation before WARNING threshold
  - WARNING triggered at t+30s
  - CRITICAL triggered at t+60s (not double-triggered)
  - Acknowledged advisory never escalates
  - Resolve() clears the track from pending
  - Tracks not in current advisory list are pruned
  - Only WEAPONS_FREE / WEAPONS_TIGHT trigger escalation
  - get_pending() returns unacknowledged states
  - reset() clears all state
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from ai import escalation as esc


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _adv(track_id: str = "T-001", engagement: str = "WEAPONS_FREE") -> dict:
    return {
        "track_id": track_id,
        "engagement": engagement,
        "urgency": "CRITICAL",
        "confidence": 80,
        "reasons": ["kill zone"],
    }


@pytest.fixture(autouse=True)
def _reset():
    esc.reset()
    yield
    esc.reset()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _check_at(advisories, seconds_elapsed):
    """Simulate check() called `seconds_elapsed` after the advisory first appeared."""
    # First call registers the advisory at 'now'
    base = time.monotonic()
    with patch("ai.escalation.time") as mock_time:
        mock_time.time.return_value = base
        esc.check(advisories)          # register
        mock_time.time.return_value = base + seconds_elapsed
        return esc.check(advisories)   # evaluate


# ── Basic threshold tests ─────────────────────────────────────────────────────

class TestThresholds:
    def test_no_escalation_before_warning(self):
        triggered = _check_at([_adv()], seconds_elapsed=10)
        assert triggered == []

    def test_warning_at_30s(self):
        triggered = _check_at([_adv()], seconds_elapsed=30)
        assert len(triggered) == 1
        assert triggered[0]["level"] == "WARNING"
        assert triggered[0]["track_id"] == "T-001"

    def test_critical_at_60s(self):
        triggered = _check_at([_adv()], seconds_elapsed=60)
        # Only CRITICAL should be triggered (escalation_level jumps to 2)
        assert len(triggered) == 1
        assert triggered[0]["level"] == "CRITICAL"

    def test_no_double_trigger_warning(self):
        base = time.monotonic()
        with patch("ai.escalation.time") as mock_time:
            mock_time.time.return_value = base
            esc.check([_adv()])
            # First check at 35s → WARNING
            mock_time.time.return_value = base + 35
            r1 = esc.check([_adv()])
            # Second check at 40s → should NOT trigger again
            mock_time.time.return_value = base + 40
            r2 = esc.check([_adv()])
        assert len(r1) == 1
        assert r2 == []

    def test_no_double_trigger_critical(self):
        base = time.monotonic()
        with patch("ai.escalation.time") as mock_time:
            mock_time.time.return_value = base
            esc.check([_adv()])
            mock_time.time.return_value = base + 65
            r1 = esc.check([_adv()])
            mock_time.time.return_value = base + 90
            r2 = esc.check([_adv()])
        assert len(r1) == 1
        assert r2 == []

    def test_warning_then_critical_separate_events(self):
        base = time.monotonic()
        with patch("ai.escalation.time") as mock_time:
            mock_time.time.return_value = base
            esc.check([_adv()])
            mock_time.time.return_value = base + 31
            r_warn = esc.check([_adv()])
            mock_time.time.return_value = base + 61
            r_crit = esc.check([_adv()])
        assert r_warn[0]["level"] == "WARNING"
        assert r_crit[0]["level"] == "CRITICAL"


# ── Acknowledgement ───────────────────────────────────────────────────────────

class TestAcknowledgement:
    def test_acknowledged_track_not_escalated(self):
        base = time.monotonic()
        with patch("ai.escalation.time") as mock_time:
            mock_time.time.return_value = base
            esc.check([_adv()])
            esc.acknowledge("T-001", "ops1")
            mock_time.time.return_value = base + 60
            triggered = esc.check([_adv()])
        assert triggered == []

    def test_acknowledge_returns_true_for_known_track(self):
        esc.check([_adv()])
        assert esc.acknowledge("T-001") is True

    def test_acknowledge_returns_false_for_unknown_track(self):
        assert esc.acknowledge("UNKNOWN") is False

    def test_acknowledged_track_absent_from_pending(self):
        esc.check([_adv()])
        esc.acknowledge("T-001", "ops1")
        pending = esc.get_pending()
        assert not any(p["track_id"] == "T-001" for p in pending)


# ── Resolve ───────────────────────────────────────────────────────────────────

class TestResolve:
    def test_resolve_removes_from_pending(self):
        esc.check([_adv()])
        esc.resolve("T-001")
        assert esc.get_pending() == []

    def test_resolve_unknown_track_no_error(self):
        esc.resolve("DOES_NOT_EXIST")   # must not raise

    def test_resolved_track_not_escalated(self):
        base = time.monotonic()
        with patch("ai.escalation.time") as mock_time:
            mock_time.time.return_value = base
            esc.check([_adv()])
            esc.resolve("T-001")
            mock_time.time.return_value = base + 60
            triggered = esc.check([_adv()])
        # resolve() pruned it; re-registering at t+60 is a fresh entry
        # (escalation_level=0 after re-register, so no trigger yet)
        assert triggered == []


# ── Pruning absent tracks ─────────────────────────────────────────────────────

class TestPruning:
    def test_track_not_in_advisories_is_pruned(self):
        esc.check([_adv("T-001")])
        # Next cycle: T-001 gone from advisories
        esc.check([_adv("T-002")])
        pending = esc.get_pending()
        ids = [p["track_id"] for p in pending]
        assert "T-001" not in ids
        assert "T-002" in ids

    def test_empty_advisories_clears_all(self):
        esc.check([_adv("T-001"), _adv("T-002")])
        esc.check([])
        assert esc.get_pending() == []


# ── Engagement filter ─────────────────────────────────────────────────────────

class TestEngagementFilter:
    def test_weapons_free_triggers(self):
        triggered = _check_at([_adv(engagement="WEAPONS_FREE")], 31)
        assert len(triggered) == 1

    def test_weapons_tight_triggers(self):
        triggered = _check_at([_adv(engagement="WEAPONS_TIGHT")], 31)
        assert len(triggered) == 1

    def test_weapons_hold_does_not_trigger(self):
        triggered = _check_at([_adv(engagement="WEAPONS_HOLD")], 31)
        assert triggered == []

    def test_track_only_does_not_trigger(self):
        triggered = _check_at([_adv(engagement="TRACK_ONLY")], 60)
        assert triggered == []

    def test_hold_fire_does_not_trigger(self):
        triggered = _check_at([_adv(engagement="HOLD_FIRE")], 60)
        assert triggered == []


# ── Multi-track ───────────────────────────────────────────────────────────────

class TestMultiTrack:
    def test_independent_clocks_per_track(self):
        base = time.monotonic()
        with patch("ai.escalation.time") as mock_time:
            mock_time.time.return_value = base
            esc.check([_adv("T-001")])
            # T-002 registers 20s later
            mock_time.time.return_value = base + 20
            esc.check([_adv("T-001"), _adv("T-002")])
            # At base+31: T-001 should warn, T-002 only 11s in — no warn
            mock_time.time.return_value = base + 31
            triggered = esc.check([_adv("T-001"), _adv("T-002")])
        ids = [t["track_id"] for t in triggered]
        assert "T-001" in ids
        assert "T-002" not in ids


# ── get_pending / reset ───────────────────────────────────────────────────────

class TestGetPendingReset:
    def test_get_pending_returns_all_unacked(self):
        esc.check([_adv("T-001"), _adv("T-002")])
        pending = esc.get_pending()
        assert len(pending) == 2

    def test_reset_clears_all(self):
        esc.check([_adv("T-001")])
        esc.reset()
        assert esc.get_pending() == []

    def test_pending_includes_duration(self):
        base = time.monotonic()
        with patch("ai.escalation.time") as mock_time:
            mock_time.time.return_value = base
            esc.check([_adv("T-001")])
            mock_time.time.return_value = base + 15
            esc.check([_adv("T-001")])
            mock_time.time.return_value = base + 20
            pending = esc.get_pending()
        assert pending[0]["duration_s"] >= 15
