"""
tests/test_scenario_runner.py — In-process scenario playback.

Pins the contract that cop.engine.scenario_runner can:
  - Load a scenario JSON by name
  - Inject cop.track + cop.threat into STATE under STATE_LOCK
  - Stop cleanly when asked
  - Refuse to start a second scenario while one is running
  - Refuse a scenario name that doesn't exist on disk
"""
from __future__ import annotations

import asyncio

import pytest

from cop.engine import scenario_runner
from cop.state import STATE


@pytest.fixture(autouse=True)
def _reset_state():
    """Each test starts from an empty STATE + idle runner."""
    STATE["tracks"].clear()
    STATE["threats"].clear()
    STATE["events_tail"].clear()
    # Drain any leftover runner state from previous tests
    scenario_runner._state.update({
        "running":      False,
        "scenario":     None,
        "started_at":   None,
        "current_tick": 0,
        "total_ticks":  0,
        "duration_s":   0.0,
        "entity_count": 0,
    })
    yield


def test_status_initially_idle():
    s = scenario_runner.status()
    assert s["running"] is False
    assert s["scenario"] is None
    assert s["current_tick"] == 0


def test_start_unknown_scenario_returns_error():
    """Bogus scenario name → error response, not raise."""
    async def _run():
        return scenario_runner.start("definitely-not-a-real-scenario")
    result = asyncio.run(_run())
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_start_injects_tracks_into_state():
    """One tick should populate STATE['tracks'] with the scenario entities."""
    async def _run():
        result = scenario_runner.start("single_drone")
        assert result["ok"] is True
        # Wait long enough for the first tick (rate_hz=1.0 → 1s, so 1.2s)
        await asyncio.sleep(1.2)
        scenario_runner.stop()
        # Wait for the loop to actually exit
        await asyncio.sleep(0.1)

    asyncio.run(_run())
    assert len(STATE["tracks"]) >= 1
    assert len(STATE["threats"]) >= 1
    # Each track should have lat/lon (polar→latlon happened)
    for tid, track in STATE["tracks"].items():
        assert "lat" in track and "lon" in track
        assert track["lat"] != 0.0  # not the origin


def test_double_start_rejected():
    """A second start() while one scenario is in flight returns an error."""
    async def _run():
        first = scenario_runner.start("single_drone")
        assert first["ok"] is True
        second = scenario_runner.start("swarm_attack")
        try:
            assert second["ok"] is False
            assert "already running" in second["error"]
        finally:
            scenario_runner.stop()
            await asyncio.sleep(0.1)

    asyncio.run(_run())


def test_stop_when_idle_is_noop():
    result = scenario_runner.stop()
    assert result["ok"] is True
    assert result.get("already_stopped") is True


def test_safe_name_strips_path_traversal():
    """Path traversal attempts must not escape scenarios/."""
    async def _run():
        result = scenario_runner.start("../../etc/passwd")
        # safe_name strips .. and slashes → becomes "etcpasswd" → not found
        assert result["ok"] is False
        assert "not found" in result["error"]
    asyncio.run(_run())
