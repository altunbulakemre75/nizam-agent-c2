"""
tests/test_clock_injection.py  —  Determinism contract tests

Locks in the Week 3 refactor: BDA's hit/miss outcome and its 30-second
pending-miss monitor must become deterministic once a FakeClock and
seeded RNG are installed, and tactical's cooldown must advance in lock
step with FakeClock. These tests fail loudly if the wall clock or the
global random module leaks back into those code paths.
"""
from __future__ import annotations

import random

import pytest

from ai import bda as ai_bda
from ai import tactical as ai_tactical
from shared.clock import FakeClock, RealClock, set_clock, reset_clock, set_rng, set_rng_seed


@pytest.fixture
def fake_clock():
    clock = FakeClock(start=1_700_000_000.0, monotonic_start=1000.0)
    set_clock(clock)
    yield clock
    reset_clock()


@pytest.fixture
def seeded_rng():
    set_rng_seed(12345)
    yield
    set_rng(random.Random())


def test_bda_outcome_is_reproducible_with_seeded_rng(seeded_rng):
    """Two runs with the same seed must produce the same hit/miss sequence."""
    ai_bda.clear()
    set_rng_seed(12345)
    outcomes_1 = [
        ai_bda.roll_outcome(f"task-{i}", f"trk-{i}", "ENGAGE", "ops1", "2026-04-11T12:00Z")
        for i in range(20)
    ]

    ai_bda.clear()
    set_rng_seed(12345)
    outcomes_2 = [
        ai_bda.roll_outcome(f"task-{i}", f"trk-{i}", "ENGAGE", "ops1", "2026-04-11T12:00Z")
        for i in range(20)
    ]

    assert outcomes_1 == outcomes_2
    # Sanity: 20 rolls at P=0.75 should never collapse to all-hit or all-miss
    assert 0 < sum(outcomes_1) < 20


def test_bda_pending_miss_resolves_deterministically(fake_clock, seeded_rng):
    """A miss registered at t=0 must become EVADED/DESTROYED_LATE at exactly t+30."""
    ai_bda.clear()
    # Hard-code a miss by forcing P=0 → every roll is a miss
    hit = ai_bda.roll_outcome(
        "task-A", "trk-A", "ENGAGE", "ops1", fake_clock.utcnow_iso(),
        hit_probability=0.0,
    )
    assert hit is False

    # Before 30s elapse, no finalisation
    fake_clock.advance(29)
    assert ai_bda.check_pending(alive_track_ids={"trk-A"}) == []

    # After 30s elapse, the track is still alive → EVADED
    fake_clock.advance(2)  # now at t+31
    finalized = ai_bda.check_pending(alive_track_ids={"trk-A"})
    assert len(finalized) == 1
    assert finalized[0]["outcome"] == "EVADED"

    # Second call after finalisation returns nothing (pending buffer drained)
    assert ai_bda.check_pending(alive_track_ids={"trk-A"}) == []


def test_bda_pending_miss_destroyed_late_when_track_gone(fake_clock, seeded_rng):
    """A miss whose track disappeared by t+30 is recorded as DESTROYED_LATE."""
    ai_bda.clear()
    ai_bda.roll_outcome(
        "task-B", "trk-B", "ENGAGE", "ops1", fake_clock.utcnow_iso(),
        hit_probability=0.0,
    )
    fake_clock.advance(31)
    finalized = ai_bda.check_pending(alive_track_ids=set())  # trk-B is gone
    assert len(finalized) == 1
    assert finalized[0]["outcome"] == "DESTROYED_LATE"


def test_tactical_cooldown_follows_fake_clock(fake_clock):
    """Tactical _should_emit cooldown must respect the injected clock."""
    ai_tactical._cooldowns.clear()
    key = "TEST:cooldown:track-1"

    # First call at t=0 emits
    assert ai_tactical._should_emit(key) is True
    # Second call at t=0 is blocked by cooldown
    assert ai_tactical._should_emit(key) is False

    # Advance less than COOLDOWN_S (30) → still blocked
    fake_clock.advance(ai_tactical.COOLDOWN_S - 5)
    assert ai_tactical._should_emit(key) is False

    # Advance past COOLDOWN_S → emits again
    fake_clock.advance(10)
    assert ai_tactical._should_emit(key) is True


def test_real_clock_is_restored_after_fixture():
    """Sanity: tests that don't install FakeClock see RealClock."""
    from shared.clock import get_clock
    assert isinstance(get_clock(), RealClock)
