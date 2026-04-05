"""
tests/test_cop_publisher.py — Tests for agents/cop_publisher.py

Covers the pieces the async refactor introduced:
- _Metrics thread-safe counters
- _enqueue_drop_oldest bounded queue behaviour (the load-shedding path
  that keeps /ingest from ever blocking the stdin reader)
- translate_track_update / translate_threat_assessment payload mapping
- polar_to_latlon coordinate conversion
"""
from __future__ import annotations

import math
import queue
import threading

import pytest

from agents.cop_publisher import (
    _Metrics,
    _enqueue_drop_oldest,
    polar_to_latlon,
    translate_threat_assessment,
    translate_track_update,
)


# ── _Metrics ────────────────────────────────────────────────────────────────

class TestMetrics:
    def test_initial_zero(self):
        m = _Metrics()
        snap = m.snapshot()
        assert snap == {"sent": 0, "failed": 0, "dropped": 0, "skipped": 0}

    def test_inc_each_field(self):
        m = _Metrics()
        m.inc("sent")
        m.inc("sent", 2)
        m.inc("failed")
        m.inc("dropped", 5)
        m.inc("skipped")
        assert m.snapshot() == {
            "sent": 3, "failed": 1, "dropped": 5, "skipped": 1,
        }

    def test_thread_safety(self):
        # 8 threads each incrementing sent 1000 times must give exactly 8000.
        # If the lock is missing, interleaved RMW will lose updates.
        m = _Metrics()
        N_THREADS = 8
        N_PER = 1000

        def worker():
            for _ in range(N_PER):
                m.inc("sent")

        threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert m.snapshot()["sent"] == N_THREADS * N_PER


# ── _enqueue_drop_oldest ────────────────────────────────────────────────────

class TestEnqueueDropOldest:
    def test_normal_enqueue_under_capacity(self):
        q: "queue.Queue[dict]" = queue.Queue(maxsize=4)
        m = _Metrics()
        for i in range(3):
            _enqueue_drop_oldest(q, {"i": i}, m)
        assert q.qsize() == 3
        assert m.snapshot()["dropped"] == 0

    def test_drops_oldest_when_full(self):
        """When the queue is at capacity, the oldest item is evicted."""
        q: "queue.Queue[dict]" = queue.Queue(maxsize=2)
        m = _Metrics()
        _enqueue_drop_oldest(q, {"i": 0}, m)
        _enqueue_drop_oldest(q, {"i": 1}, m)
        assert q.qsize() == 2
        assert m.snapshot()["dropped"] == 0

        # Third insert must evict i=0 and keep i=1, i=2
        _enqueue_drop_oldest(q, {"i": 2}, m)
        assert q.qsize() == 2
        assert m.snapshot()["dropped"] == 1

        # Drain + verify order: oldest (0) is gone
        remaining = [q.get_nowait() for _ in range(2)]
        # Mark tasks done so the queue is clean for pytest isolation
        for _ in remaining:
            q.task_done()
        # i=0 was evicted; survivors are i=1 then i=2
        assert [r["i"] for r in remaining] == [1, 2]

    def test_reader_never_blocks(self):
        """A full queue under pressure must not block the caller."""
        q: "queue.Queue[dict]" = queue.Queue(maxsize=1)
        m = _Metrics()

        # Slam 50 items into a 1-slot queue with no consumer.
        # If the reader ever blocked, this test would hang.
        for i in range(50):
            _enqueue_drop_oldest(q, {"i": i}, m)

        assert q.qsize() == 1
        # 49 should have been dropped (the first one landed in an empty queue,
        # after that every insert dropped something)
        assert m.snapshot()["dropped"] == 49


# ── polar_to_latlon ─────────────────────────────────────────────────────────

class TestPolarToLatlon:
    def test_origin_zero_range(self):
        lat, lon = polar_to_latlon(0.0, 0.0, 41.0, 29.0)
        assert lat == pytest.approx(41.0, abs=1e-6)
        assert lon == pytest.approx(29.0, abs=1e-6)

    def test_north_bearing(self):
        """1 km due north should increase lat, leave lon roughly unchanged."""
        lat, lon = polar_to_latlon(1000.0, 0.0, 41.0, 29.0)
        assert lat > 41.0
        assert lon == pytest.approx(29.0, abs=1e-6)
        # 1 km ≈ 0.00898° in latitude
        assert lat == pytest.approx(41.0 + 1000 / 111_320.0, abs=1e-6)

    def test_east_bearing(self):
        lat, lon = polar_to_latlon(1000.0, 90.0, 41.0, 29.0)
        assert lat == pytest.approx(41.0, abs=1e-6)
        assert lon > 29.0


# ── translate_track_update ──────────────────────────────────────────────────

class TestTranslateTrackUpdate:
    def test_with_kinematics_sets_latlon(self):
        payload = {
            "global_track_id": "GT-42",
            "status": "CONFIRMED",
            "classification": {"label": "drone"},
            "supporting_sensors": ["radar-01"],
            "kinematics": {"range_m": 500.0, "az_deg": 90.0,
                           "speed_mps": 30.0, "heading_deg": 180.0},
            "history": [],
            "intent": "attack",
            "intent_conf": 0.9,
        }
        out = translate_track_update(payload, origin_lat=41.0, origin_lon=29.0)
        assert out["global_track_id"] == "GT-42"
        assert out["id"] == "GT-42"
        # Due east 500m → lon grows, lat unchanged
        assert out["lat"] == pytest.approx(41.0, abs=1e-5)
        assert out["lon"] > 29.0
        assert out["intent"] == "attack"
        assert out["kinematics"]["range_m"] == 500.0

    def test_missing_kinematics_falls_back_to_origin(self):
        out = translate_track_update(
            {"global_track_id": "GT-1"},
            origin_lat=41.0, origin_lon=29.0,
        )
        assert out["lat"] == 41.0
        assert out["lon"] == 29.0
        # Passthrough defaults
        assert out["status"] == "TENTATIVE"
        assert out["intent"] == "unknown"

    def test_history_converted_to_latlon(self):
        payload = {
            "global_track_id": "GT-1",
            "kinematics": {"range_m": 0.0, "az_deg": 0.0},
            "history": [
                {"range_m": 1000.0, "az_deg": 0.0,  "ts": "t1"},
                {"range_m": 1000.0, "az_deg": 90.0, "ts": "t2"},
            ],
        }
        out = translate_track_update(payload, origin_lat=41.0, origin_lon=29.0)
        assert len(out["history"]) == 2
        # Point 1: due north → lat > 41, lon ≈ 29
        assert out["history"][0]["lat"] > 41.0
        assert out["history"][0]["lon"] == pytest.approx(29.0, abs=1e-5)
        # Point 2: due east → lat ≈ 41, lon > 29
        assert out["history"][1]["lat"] == pytest.approx(41.0, abs=1e-5)
        assert out["history"][1]["lon"] > 29.0

    def test_id_fallback_chain(self):
        # Prefers global_track_id → track_id → id → "UNKNOWN"
        out = translate_track_update({"track_id": "T-1"}, 41.0, 29.0)
        assert out["global_track_id"] == "T-1"
        out2 = translate_track_update({}, 41.0, 29.0)
        assert out2["global_track_id"] == "UNKNOWN"


# ── translate_threat_assessment ─────────────────────────────────────────────

class TestTranslateThreatAssessment:
    def test_populates_defaults(self):
        out = translate_threat_assessment({"global_track_id": "GT-1"})
        assert out["id"] == "GT-1"
        assert out["threat_level"] == "LOW"
        assert out["score"] == 0
        assert out["recommended_action"] == "OBSERVE"
        assert out["reasons"] == []

    def test_passes_through_fields(self):
        out = translate_threat_assessment({
            "global_track_id": "GT-2",
            "threat_level": "HIGH",
            "score": 88,
            "tti_s": 12.5,
            "recommended_action": "ENGAGE",
            "reasons": ["fast closing", "attack intent"],
            "rules_fired": ["R1", "R2"],
            "intent": "attack",
            "ml_probability": 0.92,
        })
        assert out["threat_level"] == "HIGH"
        assert out["score"] == 88
        assert out["tti_s"] == 12.5
        assert out["recommended_action"] == "ENGAGE"
        assert out["reasons"] == ["fast closing", "attack intent"]
        assert out["ml_probability"] == 0.92
