"""
shared/clock.py  —  Clock and RNG protocols for deterministic tests

Motivation
----------
Every `time.time()` and `random.random()` call hidden inside an AI module
turns tests into time-dependent heisenbugs. When BDA's 30-second monitor
cooldown, tactical's 30-second rule emit cooldown, and the retrainer's
feedback timestamping all read the wall clock directly, you cannot:

  - reproduce a flaky test deterministically
  - replay a scenario recording and get byte-identical output
  - assert "this rule fires at t+45" in a unit test without sleeping

The fix is to pass a `Clock` and a `Rng` into the modules that need them.
In production both are the real stdlib ones. In tests you hand them a
`FakeClock` you advance manually and a seeded `random.Random`.

Scope for this module
---------------------
  - `Clock`     — protocol: `now()`, `monotonic()`, `sleep(seconds)`
  - `RealClock` — wraps the stdlib
  - `FakeClock` — test-controlled
  - `Rng`       — protocol: same shape as random.Random
  - `default_clock()` / `set_clock()` — process-global accessor pair so
    modules that don't yet take a parameter can still opt in via the
    global without a full DI refactor
  - Same pattern for Rng

Rollout plan
------------
1. Modules that today call `time.time()` / `time.monotonic()` /
   `datetime.now(timezone.utc)` switch to `get_clock()` + `get_rng()`.
2. Tests that depend on exact timing install a `FakeClock` via `set_clock`
   in a fixture, advance time explicitly, and uninstall on teardown.
3. Over time, modules get explicit clock/rng parameters passed in at
   construction (strict DI). The process-global is the bridge that lets
   migration happen incrementally without breaking everything at once.
"""
from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable


# ── Clock protocol ────────────────────────────────────────────────────────────

@runtime_checkable
class Clock(Protocol):
    """Minimal protocol any clock must satisfy."""
    def now(self) -> float: ...
    def monotonic(self) -> float: ...
    def utcnow_iso(self) -> str: ...


class RealClock:
    """Thin wrapper around the stdlib time/datetime modules."""

    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def utcnow_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()


class FakeClock:
    """Test clock. You advance it explicitly; nothing else moves it.

    Example:
        clock = FakeClock(start=1_000_000.0)
        set_clock(clock)
        # ... run code under test ...
        clock.advance(30)  # simulate 30 seconds
        # ... check that the 30-second cooldown has expired ...
    """

    def __init__(self, start: float = 1_700_000_000.0, monotonic_start: float = 0.0):
        self._now = float(start)
        self._monotonic = float(monotonic_start)

    def now(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def utcnow_iso(self) -> str:
        return datetime.fromtimestamp(self._now, tz=timezone.utc).isoformat()

    def advance(self, seconds: float) -> None:
        """Move both wall and monotonic clocks forward."""
        self._now += float(seconds)
        self._monotonic += float(seconds)

    def set(self, wall: float) -> None:
        """Jump the wall clock to a specific timestamp. Monotonic keeps ticking."""
        self._now = float(wall)


# ── Rng protocol ──────────────────────────────────────────────────────────────

@runtime_checkable
class Rng(Protocol):
    """Minimal protocol for the random-number helpers we actually use."""
    def random(self) -> float: ...
    def uniform(self, a: float, b: float) -> float: ...
    def randint(self, a: int, b: int) -> int: ...
    def choice(self, seq): ...  # type: ignore[override]


# ── Process-global accessors (the migration bridge) ────────────────────────────

_default_clock: Clock = RealClock()
_default_rng: Rng = random.Random()


def get_clock() -> Clock:
    """Return the clock modules should use. Production default = RealClock."""
    return _default_clock


def set_clock(clock: Clock) -> None:
    """Install a clock (typically a FakeClock from a test fixture)."""
    global _default_clock
    _default_clock = clock


def reset_clock() -> None:
    """Restore RealClock. Call in test teardown."""
    global _default_clock
    _default_clock = RealClock()


def get_rng() -> Rng:
    """Return the process-wide RNG. Production default = unseeded random.Random."""
    return _default_rng


def set_rng(rng: Rng) -> None:
    """Install an RNG (seeded random.Random for tests, or a FakeRng)."""
    global _default_rng
    _default_rng = rng


def set_rng_seed(seed: int) -> None:
    """Install a freshly-seeded stdlib Random as the process RNG."""
    set_rng(random.Random(seed))


def reset_rng() -> None:
    """Restore an unseeded stdlib Random. Call in test teardown."""
    global _default_rng
    _default_rng = random.Random()


__all__ = [
    "Clock", "RealClock", "FakeClock", "get_clock", "set_clock", "reset_clock",
    "Rng", "get_rng", "set_rng", "set_rng_seed", "reset_rng",
]
