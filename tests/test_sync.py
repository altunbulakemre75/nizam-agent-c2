"""
tests/test_sync.py — Tests for cop/sync.py (distributed state synchronisation)

Covers:
  - Peer add/remove/list
  - build_delta: track window filtering, zone/asset passthrough
  - apply_delta: last-write-wins, newer record wins, older rejected
  - apply_delta: no overwrite on equal timestamps
  - /api/sync/receive endpoint (via TestClient)
  - /api/sync/status endpoint
  - reset clears all state
"""
from __future__ import annotations

import time
import pytest
from fastapi.testclient import TestClient

from cop import sync as s
from cop import server as srv


@pytest.fixture(autouse=True)
def _reset():
    s.reset()
    yield
    s.reset()


@pytest.fixture
def client():
    return TestClient(srv.app, raise_server_exceptions=False)


# ── Peer management ───────────────────────────────────────────────────────

class TestPeerManagement:
    def test_add_peer(self):
        s.add_peer("http://node2:8100")
        peers = s.list_peers()
        assert any(p["url"] == "http://node2:8100" for p in peers)

    def test_add_peer_idempotent(self):
        s.add_peer("http://node2:8100")
        s.add_peer("http://node2:8100")
        assert len(s.list_peers()) == 1

    def test_add_strips_trailing_slash(self):
        s.add_peer("http://node2:8100/")
        assert any(p["url"] == "http://node2:8100" for p in s.list_peers())

    def test_remove_peer(self):
        s.add_peer("http://node2:8100")
        removed = s.remove_peer("http://node2:8100")
        assert removed is True
        assert len(s.list_peers()) == 0

    def test_remove_nonexistent(self):
        assert s.remove_peer("http://ghost:9999") is False

    def test_stats(self):
        s.add_peer("http://a:8100")
        s.add_peer("http://b:8100")
        st = s.stats()
        assert st["peer_count"] == 2
        assert st["node_id"] == s.NODE_ID


# ── build_delta ───────────────────────────────────────────────────────────

class TestBuildDelta:
    def _state(self):
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        old_iso = datetime.fromtimestamp(time.time() - 3600, tz=timezone.utc).isoformat()
        return {
            "tracks": {
                "T-1": {"id": "T-1", "server_time": now_iso},
                "T-2": {"id": "T-2", "server_time": old_iso},
            },
            "threats": {
                "T-1": {"track_id": "T-1", "server_time": now_iso},
            },
            "zones": {
                "Z-1": {"id": "Z-1", "name": "Alpha"},
            },
            "assets": {
                "A-1": {"id": "A-1", "name": "HQ"},
            },
            "tasks": {},
            "waypoints": {},
        }

    def test_zones_always_included(self):
        state = self._state()
        delta = s.build_delta(state)
        assert "Z-1" in delta["zones"]

    def test_assets_always_included(self):
        state = self._state()
        delta = s.build_delta(state)
        assert "A-1" in delta["assets"]

    def test_fresh_tracks_included(self):
        state = self._state()
        delta = s.build_delta(state)
        # T-1 has recent server_time → should be included
        assert "T-1" in delta["tracks"]

    def test_since_filters_old_records(self):
        state = self._state()
        # since = far future → nothing passes the cutoff
        delta = s.build_delta(state, since=time.time() + 86400 * 365)
        assert "T-1" not in delta["tracks"]
        assert "T-2" not in delta["tracks"]


# ── apply_delta ───────────────────────────────────────────────────────────

class TestApplyDelta:
    def test_new_record_inserted(self):
        state: dict = {"tracks": {}, "threats": {}, "zones": {},
                       "assets": {}, "tasks": {}, "waypoints": {}}
        delta = {
            "tracks": {"T-1": {"id": "T-1", "server_time": "2026-04-10T12:00:00+00:00"}},
        }
        counts = s.apply_delta(delta, state)
        assert "T-1" in state["tracks"]
        assert counts["tracks"] == 1

    def test_newer_record_overwrites(self):
        state = {
            "tracks": {"T-1": {"id": "T-1", "lat": 41.0,
                                "server_time": "2026-04-10T11:00:00+00:00"}},
            "threats": {}, "zones": {}, "assets": {}, "tasks": {}, "waypoints": {},
        }
        delta = {
            "tracks": {"T-1": {"id": "T-1", "lat": 42.0,
                                "server_time": "2026-04-10T12:00:00+00:00"}},
        }
        s.apply_delta(delta, state)
        assert state["tracks"]["T-1"]["lat"] == 42.0

    def test_older_record_rejected(self):
        state = {
            "tracks": {"T-1": {"id": "T-1", "lat": 42.0,
                                "server_time": "2026-04-10T12:00:00+00:00"}},
            "threats": {}, "zones": {}, "assets": {}, "tasks": {}, "waypoints": {},
        }
        delta = {
            "tracks": {"T-1": {"id": "T-1", "lat": 41.0,
                                "server_time": "2026-04-10T11:00:00+00:00"}},
        }
        counts = s.apply_delta(delta, state)
        # Should NOT overwrite with older record
        assert state["tracks"]["T-1"]["lat"] == 42.0
        assert counts["tracks"] == 0

    def test_equal_timestamp_not_overwritten(self):
        ts = "2026-04-10T12:00:00+00:00"
        state = {
            "tracks": {"T-1": {"id": "T-1", "lat": 42.0, "server_time": ts}},
            "threats": {}, "zones": {}, "assets": {}, "tasks": {}, "waypoints": {},
        }
        delta = {"tracks": {"T-1": {"id": "T-1", "lat": 99.0, "server_time": ts}}}
        counts = s.apply_delta(delta, state)
        assert state["tracks"]["T-1"]["lat"] == 42.0
        assert counts["tracks"] == 0

    def test_multi_category_apply(self):
        state = {"tracks": {}, "threats": {}, "zones": {},
                 "assets": {}, "tasks": {}, "waypoints": {}}
        delta = {
            "zones":  {"Z-1": {"id": "Z-1"}},
            "assets": {"A-1": {"id": "A-1"}},
        }
        counts = s.apply_delta(delta, state)
        assert counts["zones"] == 1
        assert counts["assets"] == 1


# ── /api/sync/receive endpoint ────────────────────────────────────────────

class TestSyncReceiveEndpoint:
    def test_valid_delta_applied(self, client):
        resp = client.post("/api/sync/receive", json={
            "node_id":   "cop-node-02",
            "pushed_at": "2026-04-10T12:00:00+00:00",
            "delta": {
                "zones": {
                    "Z-SYNC": {
                        "id": "Z-SYNC", "name": "Sync Zone",
                        "server_time": "2026-04-10T12:00:00+00:00",
                    }
                },
            },
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["applied"]["zones"] >= 1

    def test_missing_delta_rejected(self, client):
        resp = client.post("/api/sync/receive", json={"node_id": "x"})
        assert resp.status_code == 400

    def test_invalid_json_rejected(self, client):
        resp = client.post("/api/sync/receive",
                           content=b"not json",
                           headers={"content-type": "application/json"})
        assert resp.status_code == 400


# ── /api/sync/status endpoint ─────────────────────────────────────────────

class TestSyncStatusEndpoint:
    def test_status_returns_node_id(self, client):
        resp = client.get("/api/sync/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "node_id" in data
        assert "peer_count" in data
