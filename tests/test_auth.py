"""JWT auth tests."""
from __future__ import annotations

import os
import time

import pytest


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch):
    monkeypatch.setenv("NIZAM_JWT_SECRET", "test-secret-key-min-16-chars")
    yield


def test_issue_and_verify():
    from shared.auth import issue_token, verify_token

    token = issue_token("opr-01", role="operator")
    payload = verify_token(token)
    assert payload["sub"] == "opr-01"
    assert payload["role"] == "operator"


def test_expired_token_raises():
    from shared.auth import AuthError, issue_token, verify_token

    token = issue_token("opr-01", ttl_s=-1)   # already expired
    with pytest.raises(AuthError, match="expired"):
        verify_token(token)


def test_invalid_signature_raises(monkeypatch):
    from shared.auth import AuthError, issue_token, verify_token

    token = issue_token("opr-01")
    monkeypatch.setenv("NIZAM_JWT_SECRET", "different-secret-same-length!!")
    with pytest.raises(AuthError):
        verify_token(token)


def test_missing_secret_raises(monkeypatch):
    from shared.auth import AuthError, issue_token

    monkeypatch.delenv("NIZAM_JWT_SECRET", raising=False)
    with pytest.raises(AuthError, match="NIZAM_JWT_SECRET"):
        issue_token("opr-01")


def test_short_secret_raises(monkeypatch):
    from shared.auth import AuthError, issue_token

    monkeypatch.setenv("NIZAM_JWT_SECRET", "short")
    with pytest.raises(AuthError, match="kısa"):
        issue_token("opr-01")
