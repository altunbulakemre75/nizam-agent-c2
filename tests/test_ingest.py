"""
tests/test_ingest.py — Tests for the /ingest endpoint in cop/server.py

Covers:
  - Valid event types (cop.track, cop.threat)
  - Validation: missing fields, unknown event_type, invalid JSON
  - Payload size guard (413)
  - Rate limiting (429)
  - Track state update + FSM integration
  - Event tail recording
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cop import server as srv


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset server state between tests."""
    srv.STATE["tracks"].clear()
    srv.STATE["threats"].clear()
    srv.STATE["zones"].clear()
    srv.STATE["assets"].clear()
    srv.STATE["tasks"].clear()
    srv.STATE["waypoints"].clear()
    srv.STATE["events_tail"].clear()
    srv.METRICS["ingest_total"] = 0
    srv.METRICS["ingest_bad_request"] = 0
    srv.METRICS["ingest_by_type"] = {}
    srv._rate_buckets.clear()
    yield


@pytest.fixture
def client():
    return TestClient(srv.app, raise_server_exceptions=False)


# ── Valid ingest ──────────────────────────────────────────────────────────

class TestValidIngest:
    def test_track_ingest(self, client):
        resp = client.post("/ingest", json={
            "event_type": "cop.track",
            "payload": {
                "id": "T-001",
                "lat": 41.02, "lon": 28.98,
                "speed": 35.0, "heading": 180.0,
                "classification": {"label": "drone"},
                "supporting_sensors": ["radar-01"],
            },
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert "T-001" in srv.STATE["tracks"]

    def test_threat_ingest(self, client):
        resp = client.post("/ingest", json={
            "event_type": "cop.threat",
            "payload": {
                "track_id": "T-001",
                "threat_level": "HIGH",
                "score": 85,
                "intent": "attack",
            },
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert "T-001" in srv.STATE["threats"]

    def test_zone_ingest(self, client):
        resp = client.post("/ingest", json={
            "event_type": "cop.zone",
            "payload": {
                "id": "zone-alpha",
                "name": "Alpha",
                "type": "restricted",
                "coordinates": [[41.0, 29.0], [41.0, 29.1], [41.1, 29.1], [41.1, 29.0]],
            },
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_metrics_increment(self, client):
        client.post("/ingest", json={
            "event_type": "cop.track",
            "payload": {"id": "T-X", "lat": 41.0, "lon": 29.0},
        })
        assert srv.METRICS["ingest_total"] >= 1
        assert srv.METRICS["ingest_by_type"].get("cop.track", 0) >= 1

    def test_server_time_injected(self, client):
        client.post("/ingest", json={
            "event_type": "cop.track",
            "payload": {"id": "T-ST", "lat": 41.0, "lon": 29.0},
        })
        track = srv.STATE["tracks"].get("T-ST", {})
        assert "server_time" in track

    def test_track_fsm_state_added(self, client):
        client.post("/ingest", json={
            "event_type": "cop.track",
            "payload": {
                "id": "T-FSM",
                "lat": 41.0, "lon": 29.0,
                "supporting_sensors": ["radar-01", "eo-01"],
            },
        })
        track = srv.STATE["tracks"].get("T-FSM", {})
        assert "track_state" in track
        # 2 sensors → should promote to TRACKED
        assert track["track_state"] == "TRACKED"


# ── Validation / rejection ────────────────────────────────────────────────

class TestIngestValidation:
    def test_missing_event_type(self, client):
        resp = client.post("/ingest", json={"payload": {"id": "T-1"}})
        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_missing_payload(self, client):
        resp = client.post("/ingest", json={"event_type": "cop.track"})
        assert resp.status_code == 400

    def test_unknown_event_type(self, client):
        resp = client.post("/ingest", json={
            "event_type": "cop.unknown_thing",
            "payload": {"id": "T-1"},
        })
        assert resp.status_code == 400
        assert "unknown event_type" in resp.json()["error"]

    def test_invalid_json(self, client):
        resp = client.post("/ingest", content=b"not json",
                           headers={"content-type": "application/json"})
        assert resp.status_code == 400

    def test_bad_request_metric(self, client):
        before = srv.METRICS["ingest_bad_request"]
        client.post("/ingest", json={"event_type": "cop.bogus", "payload": {}})
        assert srv.METRICS["ingest_bad_request"] > before

    def test_payload_too_large(self, client):
        resp = client.post("/ingest",
                           json={"event_type": "cop.track", "payload": {"id": "T-1"}},
                           headers={"content-length": "999999"})
        assert resp.status_code == 413


# ── Rate limiting ─────────────────────────────────────────────────────────

class TestRateLimit:
    def test_rate_limit_allows_normal_traffic(self):
        assert srv._rate_limit_check("10.0.0.1") is True

    def test_rate_limit_blocks_after_burst(self):
        ip = "10.0.0.99"
        # Exhaust burst capacity
        for _ in range(srv._RATE_LIMIT_BURST + 50):
            srv._rate_limit_check(ip)
        # Should be blocked now
        assert srv._rate_limit_check(ip) is False


# ── Event tail ────────────────────────────────────────────────────────────

# ── API key auth guard ─────────────────────────────────────────────────────

class TestIngestApiKeyAuth:
    def test_no_key_required_when_auth_disabled(self, client, monkeypatch):
        monkeypatch.setattr(srv, "AUTH_ENABLED", False)
        resp = client.post("/ingest", json={
            "event_type": "cop.track",
            "payload": {"id": "T-AK1", "lat": 41.0, "lon": 29.0},
        })
        assert resp.status_code == 200

    def test_rejected_without_key_when_auth_enabled(self, client, monkeypatch):
        monkeypatch.setattr(srv, "AUTH_ENABLED", True)
        monkeypatch.setattr(srv, "INGEST_API_KEY", "secret-key-123")
        resp = client.post("/ingest", json={
            "event_type": "cop.track",
            "payload": {"id": "T-AK2", "lat": 41.0, "lon": 29.0},
        })
        assert resp.status_code == 401

    def test_rejected_with_wrong_key(self, client, monkeypatch):
        monkeypatch.setattr(srv, "AUTH_ENABLED", True)
        monkeypatch.setattr(srv, "INGEST_API_KEY", "secret-key-123")
        resp = client.post("/ingest",
                           json={"event_type": "cop.track", "payload": {"id": "T-AK3"}},
                           headers={"x-api-key": "wrong-key"})
        assert resp.status_code == 401

    def test_accepted_with_correct_key(self, client, monkeypatch):
        monkeypatch.setattr(srv, "AUTH_ENABLED", True)
        monkeypatch.setattr(srv, "INGEST_API_KEY", "secret-key-123")
        resp = client.post("/ingest",
                           json={"event_type": "cop.track",
                                 "payload": {"id": "T-AK4", "lat": 41.0, "lon": 29.0}},
                           headers={"x-api-key": "secret-key-123"})
        assert resp.status_code == 200

    def test_no_key_env_means_no_guard(self, client, monkeypatch):
        """AUTH_ENABLED=true but INGEST_API_KEY not set → no key check."""
        monkeypatch.setattr(srv, "AUTH_ENABLED", True)
        monkeypatch.setattr(srv, "INGEST_API_KEY", "")
        resp = client.post("/ingest", json={
            "event_type": "cop.track",
            "payload": {"id": "T-AK5", "lat": 41.0, "lon": 29.0},
        })
        assert resp.status_code == 200


# ── Event tail ────────────────────────────────────────────────────────────

class TestEventTail:
    def test_events_recorded(self, client):
        client.post("/ingest", json={
            "event_type": "cop.track",
            "payload": {"id": "T-1", "lat": 41.0, "lon": 29.0},
        })
        assert len(srv.STATE["events_tail"]) >= 1
        assert srv.STATE["events_tail"][-1]["event_type"] == "cop.track"
