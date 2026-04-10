"""
tests/test_server_tactical.py — Tests for the tactical offload path in
cop/server.py.

These tests cover the functions added in the async-refactor that moved
the tactical engine off the asyncio event loop:
- _ai_run_tactical_compute: pure-compute pass, takes snapshots
- _schedule_ai_tactical: rate-limited fire-and-forget scheduler
- _metrics_record_tactical_duration / _metrics_percentile: metrics helpers

We do NOT spin up a FastAPI test client — the goal is to pin the logic
of the refactor, not re-test FastAPI itself.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from cop import server as srv


@pytest.fixture(autouse=True)
def _reset_tactical_state():
    """Reset module-level state so tests don't bleed into each other."""
    srv._ai_tactical_last = 0.0
    srv.METRICS["tactical_scheduled"]      = 0
    srv.METRICS["tactical_rate_skipped"]   = 0
    srv.METRICS["tactical_ran"]            = 0
    srv.METRICS["tactical_overlap_skipped"] = 0
    srv.METRICS["tactical_failed"]         = 0
    srv.METRICS["tactical_last_ms"]        = 0.0
    srv.METRICS["tactical_max_ms"]         = 0.0
    srv.METRICS["tactical_recent_ms"]      = []
    yield


# ── _ai_run_tactical_compute: pure-compute pass ─────────────────────────────

class TestRunTacticalCompute:
    def test_empty_state_returns_structured_result(self):
        """Zero tracks/threats/assets/zones must still return the expected shape."""
        result = srv._ai_run_tactical_compute({}, {}, {}, {})

        required_keys = {
            "swarm_anomalies",
            "recommendations",
            "pred_breaches",
            "uncertainty_cones",
            "coord_attacks",
            "ml_predictions",
            "roe_advisories",
        }
        assert required_keys.issubset(set(result.keys()))
        # All list fields must actually be lists (caller does .extend)
        assert isinstance(result["swarm_anomalies"], list)
        assert isinstance(result["recommendations"], list)
        assert isinstance(result["pred_breaches"], list)
        assert isinstance(result["coord_attacks"], list)
        assert isinstance(result["roe_advisories"], list)
        assert isinstance(result["uncertainty_cones"], dict)
        assert isinstance(result["ml_predictions"], dict)

    def test_does_not_mutate_input_snapshots(self):
        """
        The compute pass runs in a thread pool, so it MUST NOT mutate its
        input snapshots (the caller's dicts). Otherwise we introduce a data
        race against /ingest, which owns the real STATE dicts.
        """
        tracks   = {"T-1": {"id": "T-1", "lat": 41.0, "lon": 29.0,
                            "classification": {"label": "drone"}}}
        threats  = {"T-1": {"id": "T-1", "threat_level": "HIGH", "score": 80}}
        assets   = {"A-1": {"id": "A-1", "lat": 41.015, "lon": 28.979,
                            "type": "friendly"}}
        zones: dict = {}

        tracks_copy  = {k: dict(v) for k, v in tracks.items()}
        threats_copy = {k: dict(v) for k, v in threats.items()}
        assets_copy  = {k: dict(v) for k, v in assets.items()}
        zones_copy   = dict(zones)

        srv._ai_run_tactical_compute(tracks, threats, assets, zones)

        # Top-level keys unchanged
        assert tracks.keys()  == tracks_copy.keys()
        assert threats.keys() == threats_copy.keys()
        assert assets.keys()  == assets_copy.keys()
        assert zones.keys()   == zones_copy.keys()
        # Inner dict contents unchanged
        assert tracks["T-1"]  == tracks_copy["T-1"]
        assert threats["T-1"] == threats_copy["T-1"]


# ── _schedule_ai_tactical: rate limit + fire-and-forget ─────────────────────

class TestScheduleTactical:
    """
    _schedule_ai_tactical calls asyncio.create_task internally, which
    requires a running event loop. Each test wraps its body in an
    asyncio.run() to provide one, then cancels the spawned task so it
    doesn't linger.
    """

    def _drain(self):
        """Best-effort: cancel any pending background tactical task so it
        doesn't warn about 'coroutine was never awaited' when the loop ends."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for task in asyncio.all_tasks(loop):
            if task is not asyncio.current_task():
                task.cancel()

    def test_first_call_schedules(self):
        async def _body():
            # Start with last=0 so the first call must pass the gate.
            srv._ai_tactical_last = 0.0
            assert srv._schedule_ai_tactical() is True
            assert srv.METRICS["tactical_scheduled"] == 1
            assert srv.METRICS["tactical_rate_skipped"] == 0
            self._drain()
            # Let cancelled tasks settle before the loop closes
            await asyncio.sleep(0)

        asyncio.run(_body())

    def test_rate_limit_skips_next_call(self):
        """Back-to-back calls within the interval must be rate-limited."""
        async def _body():
            srv._ai_tactical_last = time.time()  # just ran
            assert srv._schedule_ai_tactical() is False
            assert srv.METRICS["tactical_scheduled"] == 1
            assert srv.METRICS["tactical_rate_skipped"] == 1
            # No task was scheduled (rate-limited), nothing to drain.

        asyncio.run(_body())

    def test_schedules_again_after_interval(self, monkeypatch):
        """
        After the interval elapses, the next call must schedule again.
        We fake time.time() inside the scheduler to avoid waiting.
        """
        async def _body():
            srv._ai_tactical_last = 1000.0

            # _schedule_ai_tactical imports 'time as _time' locally; patch
            # the time module that function actually sees.
            import time as real_time
            fake = {"t": 1000.0 + srv._AI_TACTICAL_INTERVAL + 0.1}
            monkeypatch.setattr(real_time, "time", lambda: fake["t"])

            assert srv._schedule_ai_tactical() is True
            # And subsequent immediate call is rate-skipped
            assert srv._schedule_ai_tactical() is False
            assert srv.METRICS["tactical_rate_skipped"] == 1
            self._drain()
            await asyncio.sleep(0)

        asyncio.run(_body())


# ── Metrics helpers ─────────────────────────────────────────────────────────

class TestMetricsHelpers:
    def test_record_tactical_duration_updates_last_and_max(self):
        srv._metrics_record_tactical_duration(123.4)
        assert srv.METRICS["tactical_last_ms"] == 123.4
        assert srv.METRICS["tactical_max_ms"]  == 123.4

        srv._metrics_record_tactical_duration(50.0)
        assert srv.METRICS["tactical_last_ms"] == 50.0
        # Max must NOT regress
        assert srv.METRICS["tactical_max_ms"]  == 123.4

        srv._metrics_record_tactical_duration(999.9)
        assert srv.METRICS["tactical_last_ms"] == 999.9
        assert srv.METRICS["tactical_max_ms"]  == 999.9

    def test_record_tactical_duration_rolling_window(self):
        """Rolling window must cap at _TACTICAL_RECENT_MAX entries."""
        for i in range(srv._TACTICAL_RECENT_MAX + 10):
            srv._metrics_record_tactical_duration(float(i))
        recent = srv.METRICS["tactical_recent_ms"]
        assert len(recent) == srv._TACTICAL_RECENT_MAX
        # The oldest 10 should have been evicted; first entry is 10
        assert recent[0] == 10.0
        assert recent[-1] == float(srv._TACTICAL_RECENT_MAX + 9)

    def test_percentile_empty_returns_zero(self):
        assert srv._metrics_percentile([], 50) == 0.0
        assert srv._metrics_percentile([], 95) == 0.0

    def test_percentile_basic(self):
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        # p0 → smallest, p100 → largest
        assert srv._metrics_percentile(values, 0)   == 10.0
        assert srv._metrics_percentile(values, 100) == 50.0
        # p50 on 5 values: index round(0.5*4)=2 → 30.0
        assert srv._metrics_percentile(values, 50) == 30.0


# ── Background task integration (sync via asyncio.run) ──────────────────────

class TestBackgroundTaskIntegration:
    def test_background_task_runs_and_records_metrics(self):
        """
        End-to-end: the background task should snapshot empty state, run
        the (lightweight) compute pass, record metrics, and broadcast.
        With zero WS clients and empty state, it should complete in under
        a second and increment tactical_ran by 1.
        """
        # Start from a clean slate
        assert srv.METRICS["tactical_ran"] == 0

        async def _run():
            await srv._ai_tactical_background_task()

        asyncio.run(_run())

        assert srv.METRICS["tactical_ran"] == 1
        assert srv.METRICS["tactical_last_ms"] > 0.0
        assert len(srv.METRICS["tactical_recent_ms"]) == 1

    def test_background_task_overlap_guard(self):
        """
        If a second call arrives while the lock is held, it must drop
        the tick and NOT run the compute pass twice.
        """
        assert srv.METRICS["tactical_ran"] == 0
        assert srv.METRICS["tactical_overlap_skipped"] == 0

        async def _run():
            # Manually acquire the bg lock to simulate an in-flight pass,
            # then call the background task — it must see the lock held
            # and return immediately.
            async with srv._ai_tactical_bg_lock:
                await srv._ai_tactical_background_task()

        asyncio.run(_run())

        assert srv.METRICS["tactical_ran"] == 0
        assert srv.METRICS["tactical_overlap_skipped"] == 1
