"""WebSocket gateway JWT auth tests."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("NIZAM_JWT_SECRET", "test-secret-key-min-16-chars")
    from services.gateway.track_gateway import app

    return TestClient(app)


def test_missing_token_rejected(client):
    """Token olmadan WebSocket bağlantısı 4401 ile kapatılmalı."""
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/tracks"):
            pass


def test_invalid_token_rejected(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/tracks?token=bogus"):
            pass


def test_valid_token_connects(client):
    from shared.auth import issue_token

    token = issue_token("opr-01", role="operator")
    # Connect başarılı olmalı; hub.connect() snapshot gönderir
    with client.websocket_connect(f"/ws/tracks?token={token}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "snapshot"


def test_auth_disabled_env_allows_anonymous(client, monkeypatch):
    monkeypatch.setenv("NIZAM_WS_AUTH_DISABLED", "true")
    # AUTH kapalı → tokensiz geçmeli
    with client.websocket_connect("/ws/tracks") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "snapshot"
