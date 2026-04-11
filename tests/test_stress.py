"""
tests/test_stress.py — Load / latency regression harness

Until now the 516-test suite proved correctness but never measured behaviour
under load. These tests exercise the hot paths (tactical compute, multi-
effector assignment, HTTP ingest) at sizes that mimic a realistic swarm
scenario and fail loudly if anyone reintroduces an O(n³)-in-pure-Python
style regression.

Marked with @pytest.mark.slow so CI can skip by default via `-m "not slow"`
but the numbers still run locally as a guardrail.

Pass/fail thresholds are deliberately loose (3-5× typical observed time) so
the suite is not flaky on slow runners — the goal is catching blowups, not
micro-benchmarking.
"""
from __future__ import annotations

import asyncio
import random
import statistics
import time
from typing import Dict, List

import pytest

from ai import assignment as ai_assignment
from cop import server as srv
import cop.routers.ingest as _ingest_mod


pytestmark = pytest.mark.slow


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_tracks(n: int, seed: int = 7) -> Dict[str, Dict]:
    r = random.Random(seed)
    tracks = {}
    for i in range(n):
        tid = f"ST-{i:04d}"
        tracks[tid] = {
            "id": tid,
            "global_track_id": tid,
            "lat": 41.0 + r.random() * 0.4,
            "lon": 29.0 + r.random() * 0.4,
            "status": "CONFIRMED",
            "classification": {"label": "drone", "confidence": 0.8},
            "supporting_sensors": ["radar"],
            "kinematics": {
                "speed_mps":   10.0 + r.random() * 40.0,
                "heading_deg": r.random() * 360.0,
                "altitude_m":  50.0 + r.random() * 800.0,
            },
            "intent":      r.choice(["attack", "recon", "unknown"]),
            "intent_conf": round(r.random(), 2),
        }
    return tracks


def _make_threats(tracks: Dict[str, Dict], seed: int = 7) -> Dict[str, Dict]:
    r = random.Random(seed + 1)
    return {
        tid: {
            "id": tid,
            "threat_level": r.choice(["LOW", "MEDIUM", "HIGH"]),
            "score":         r.randint(30, 95),
        }
        for tid in tracks
    }


def _make_assets(n: int, seed: int = 11) -> Dict[str, Dict]:
    r = random.Random(seed)
    assets = {}
    for i in range(n):
        aid = f"E-{i:03d}"
        assets[aid] = {
            "id": aid,
            "name": f"Interceptor-{i}",
            "type": "interceptor",
            "status": "active",
            "lat": 41.0 + r.random() * 0.4,
            "lon": 29.0 + r.random() * 0.4,
            "range_km": 50.0,
        }
    return assets


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((pct / 100.0) * (len(s) - 1)))))
    return s[k]


# ── Tactical loop latency under load ──────────────────────────────────────────

class TestTacticalLatencyUnderLoad:
    """
    Runs the full _ai_run_tactical_compute pipeline with increasingly large
    track counts and asserts that p99 latency stays inside sane bounds.
    """

    @pytest.mark.parametrize("n_tracks,max_p95_ms", [
        (10,  1500),
        (50,  3000),
        (100, 6000),
    ])
    def test_tactical_compute_scales(self, n_tracks, max_p95_ms):
        tracks  = _make_tracks(n_tracks)
        threats = _make_threats(tracks)
        assets  = _make_assets(8)
        zones: Dict = {}

        # Warmup: first 2 runs load ML/LSTM models into memory and prime the
        # thread pool. Measuring them would just record the cold-start cost.
        for _ in range(2):
            srv._ai_run_tactical_compute(tracks, threats, assets, zones)

        samples: List[float] = []
        for _ in range(10):
            t0 = time.perf_counter()
            result = srv._ai_run_tactical_compute(tracks, threats, assets, zones)
            samples.append((time.perf_counter() - t0) * 1000.0)
            assert "ml_predictions" in result
            assert "roe_advisories" in result

        p50 = statistics.median(samples)
        p95 = _percentile(samples, 95)

        print(
            f"\n  tactical[{n_tracks}] p50={p50:7.1f}ms  "
            f"p95={p95:7.1f}ms  samples={len(samples)}"
        )

        assert p95 < max_p95_ms, (
            f"Tactical p95 regression: {p95:.0f}ms > {max_p95_ms}ms "
            f"at {n_tracks} tracks"
        )


# ── Assignment engine — this is the test that would have caught F1 ───────────

class TestAssignmentScaling:
    """
    Pins scipy's linear_sum_assignment behaviour. The old pure-Python
    Hungarian blew up to 9+ seconds at 150x150; these thresholds assume
    the C implementation and fail if anyone accidentally reverts it.
    """

    @pytest.mark.parametrize("n_threats,n_effectors,max_ms", [
        (40,  40,  200),
        (100, 100, 500),
        (150, 150, 1000),
    ])
    def test_assignment_scales_sublinear_in_seconds(self, n_threats, n_effectors, max_ms):
        tracks  = _make_tracks(n_threats)
        threats = _make_threats(tracks)
        assets  = _make_assets(n_effectors)
        roe_advisories = [
            {"track_id": tid, "engagement": "WEAPONS_FREE"} for tid in tracks
        ]

        samples: List[float] = []
        for _ in range(5):
            t0 = time.perf_counter()
            result = ai_assignment.compute(threats, assets, roe_advisories)
            samples.append((time.perf_counter() - t0) * 1000.0)
            assert isinstance(result.assignments, list)

        p99 = _percentile(samples, 99)
        print(f"\n  assign[{n_threats}x{n_effectors}] p99={p99:7.1f}ms")
        assert p99 < max_ms, (
            f"Assignment p99 regression at {n_threats}x{n_effectors}: "
            f"{p99:.0f}ms > {max_ms}ms — did the scipy Hungarian get reverted?"
        )


# ── Concurrent ingest throughput via direct handler call ────────────────────

class TestIngestConcurrency:
    """
    Fires N concurrent calls at the /ingest handler via asyncio.gather in a
    single event loop — the same concurrency model production runs under
    (uvicorn has one loop, many in-flight requests). This catches races in
    STATE mutation and makes sure the async lock actually serialises
    writers without deadlocking or swallowing events.

    We bypass the FastAPI TestClient + threads combination because its
    per-thread event loop binding conflicts with the module-level
    asyncio.Lock — an artefact of the test harness, not production.
    """

    def test_parallel_ingest_no_errors(self):
        from fastapi import Request

        n_events = 200
        parallelism = 20

        async def _fake_request(i: int) -> Request:
            # Spread tracks across a ~20km grid so deconfliction doesn't fuse
            # them into a handful of canonical tracks — we want to measure
            # the ingest pipeline, not the fuser's merge behaviour.
            row = i // 20
            col = i % 20
            body = {
                "event_type": "cop.track",
                "payload": {
                    "id":  f"ST-{i:04d}",
                    "lat": 41.0 + row * 0.02,
                    "lon": 29.0 + col * 0.02,
                    "speed":   20.0,
                    "heading": (i * 3.7) % 360,
                    "classification": {"label": "drone", "confidence": 0.8},
                    "supporting_sensors": ["radar"],
                },
            }
            import json
            body_bytes = json.dumps(body).encode()

            scope = {
                "type": "http",
                "method": "POST",
                "path": "/ingest",
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body_bytes)).encode()),
                ],
                "client": (f"10.0.0.{i % 200}", 12345),
                "query_string": b"",
            }
            sent = {"body": False}

            async def receive():
                if sent["body"]:
                    return {"type": "http.disconnect"}
                sent["body"] = True
                return {
                    "type": "http.request",
                    "body": body_bytes,
                    "more_body": False,
                }

            return Request(scope, receive)

        async def _one(i: int) -> int:
            req = await _fake_request(i)
            resp = await _ingest_mod.ingest(req)
            return resp.status_code

        async def _run():
            sem = asyncio.Semaphore(parallelism)

            async def _guarded(i):
                async with sem:
                    return await _one(i)

            return await asyncio.gather(*[_guarded(i) for i in range(n_events)])

        # Clean state for deterministic counts
        srv.STATE["tracks"].clear()

        t0 = time.perf_counter()
        statuses = asyncio.run(_run())
        elapsed = time.perf_counter() - t0

        ok = sum(1 for s in statuses if 200 <= s < 300)
        err = n_events - ok
        throughput = n_events / elapsed if elapsed > 0 else 0.0

        print(
            f"\n  ingest[{n_events}x{parallelism}] ok={ok} err={err} "
            f"elapsed={elapsed*1000:.0f}ms throughput={throughput:.0f}/s"
        )

        assert ok >= int(n_events * 0.98), f"Too many ingest errors: {err}/{n_events}"
        assert len(srv.STATE["tracks"]) >= int(n_events * 0.98), (
            f"Not enough tracks persisted: {len(srv.STATE['tracks'])}/{n_events}"
        )
