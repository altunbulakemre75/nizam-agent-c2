"""
tests/test_ws_auth.py — Tests for WebSocket authentication in cop/server.py

Covers:
  - WS connection succeeds when AUTH_ENABLED=false
  - WS connection with valid token when AUTH_ENABLED=true
  - WS connection rejected (4001) with invalid/missing token when AUTH_ENABLED=true
  - Operator ID assignment
  - Snapshot delivery on connect
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cop import server as srv


@pytest.fixture(autouse=True)
def _reset_state():
    srv.CLIENTS.clear()
    srv.OPERATORS.clear()
    srv.WS_OPERATORS.clear()
    srv.TRACK_CLAIMS.clear()
    yield
    srv.CLIENTS.clear()
    srv.OPERATORS.clear()
    srv.WS_OPERATORS.clear()
    srv.TRACK_CLAIMS.clear()


@pytest.fixture
def client():
    return TestClient(srv.app)


class TestWSNoAuth:
    """When AUTH_ENABLED=false, any WS connection should work."""

    def test_connect_without_token(self, client, monkeypatch):
        monkeypatch.setattr(srv, "AUTH_ENABLED", False)
        with client.websocket_connect("/ws") as ws:
            # First message is operator_joined broadcast, second is snapshot
            join = ws.receive_json()
            assert join["event_type"] == "cop.operator_joined"
            snapshot = ws.receive_json()
            assert snapshot["event_type"] == "cop.snapshot"

    def test_connect_with_operator_id(self, client, monkeypatch):
        monkeypatch.setattr(srv, "AUTH_ENABLED", False)
        with client.websocket_connect("/ws?operator_id=OPS-TEST") as ws:
            join = ws.receive_json()
            assert join["event_type"] == "cop.operator_joined"
            snapshot = ws.receive_json()
            assert snapshot["event_type"] == "cop.snapshot"
            # Operator should appear in the operators list
            ops = snapshot["payload"].get("operators", [])
            op_ids = [o["operator_id"] for o in ops]
            assert "OPS-TEST" in op_ids


class TestWSWithAuth:
    """When AUTH_ENABLED=true, token validation must be enforced."""

    def test_reject_missing_token(self, client, monkeypatch):
        monkeypatch.setattr(srv, "AUTH_ENABLED", True)
        with pytest.raises(Exception):
            # Should close with code 4001
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()

    def test_reject_invalid_token(self, client, monkeypatch):
        monkeypatch.setattr(srv, "AUTH_ENABLED", True)
        with pytest.raises(Exception):
            with client.websocket_connect("/ws?token=garbage-token") as ws:
                ws.receive_json()

    def test_accept_valid_token(self, client, monkeypatch):
        monkeypatch.setattr(srv, "AUTH_ENABLED", True)
        # Create a valid JWT
        from auth.deps import create_access_token
        token = create_access_token({"sub": "test-operator"})
        with client.websocket_connect(f"/ws?token={token}") as ws:
            join = ws.receive_json()
            assert join["event_type"] == "cop.operator_joined"
            snapshot = ws.receive_json()
            assert snapshot["event_type"] == "cop.snapshot"
