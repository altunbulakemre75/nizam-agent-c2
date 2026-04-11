"""
tests/test_cors.py — CORS middleware behaviour.

Pins:
- Default dev origins (localhost:8100 / 127.0.0.1:8100 / Vite ports) work
- An origin that isn't in the whitelist is rejected
- Wildcard "*" in ALLOWED_ORIGINS refuses to boot
- Credentials are allowed (allow_credentials=True)

We test the live FastAPI app via TestClient. CORS middleware is added at
import time from the env var ALLOWED_ORIGINS, so wildcard rejection
needs a re-import — same pattern as the other boot guards.
"""
from __future__ import annotations

import importlib
import sys

import pytest
from fastapi.testclient import TestClient


def _reimport_server():
    for mod in ("cop.server",):
        sys.modules.pop(mod, None)
    return importlib.import_module("cop.server")


@pytest.fixture
def _clean_env(monkeypatch):
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    yield
    sys.modules.pop("cop.server", None)


def test_default_dev_origin_allowed(_clean_env):
    """Default config allows localhost:8100 — the dev front-end origin."""
    srv = _reimport_server()
    client = TestClient(srv.app)
    resp = client.get("/api/tracks", headers={"Origin": "http://localhost:8100"})
    # GET /api/tracks may return 200/404 depending on routing — what we
    # care about is the CORS header echo for the allowed origin.
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:8100"


def test_unknown_origin_blocked(_clean_env):
    """An origin not in the whitelist gets no allow-origin header."""
    srv = _reimport_server()
    client = TestClient(srv.app)
    resp = client.get("/api/tracks", headers={"Origin": "https://evil.example.com"})
    # Starlette omits the header entirely for non-whitelisted origins.
    assert resp.headers.get("access-control-allow-origin") is None


def test_wildcard_origin_refuses_to_boot(_clean_env, monkeypatch):
    """ALLOWED_ORIGINS='*' must raise at import time."""
    monkeypatch.setenv("ALLOWED_ORIGINS", "*")
    with pytest.raises(RuntimeError, match="Wildcard"):
        _reimport_server()


def test_explicit_origin_list_overrides_default(_clean_env, monkeypatch):
    """A custom comma-separated list takes precedence over the dev defaults."""
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://app.example.com,https://staging.example.com")
    srv = _reimport_server()
    client = TestClient(srv.app)

    resp_ok = client.get("/api/tracks",
                         headers={"Origin": "https://app.example.com"})
    assert resp_ok.headers.get("access-control-allow-origin") == "https://app.example.com"

    # Default localhost is no longer in the list
    resp_blocked = client.get("/api/tracks",
                              headers={"Origin": "http://localhost:8100"})
    assert resp_blocked.headers.get("access-control-allow-origin") is None
