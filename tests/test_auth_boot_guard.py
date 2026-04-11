"""
tests/test_auth_boot_guard.py — Boot-time JWT_SECRET guard.

Pins the contract that auth/deps.py refuses to import when
AUTH_ENABLED=true and JWT_SECRET is missing or still the public default.
This prevents the "operator flipped AUTH_ENABLED but forgot to set the
secret, now every attacker can mint tokens" failure mode.

We test by reloading the module under controlled environment variables.
"""
from __future__ import annotations

import importlib
import os
import sys

import pytest


_DEFAULT_SECRET = "nizam-dev-secret-CHANGE-in-production"


def _reimport_auth_deps():
    """Force-reimport auth.deps so the module-level guard re-runs."""
    sys.modules.pop("auth.deps", None)
    return importlib.import_module("auth.deps")


@pytest.fixture
def _clean_env(monkeypatch):
    """Strip AUTH_ENABLED + JWT_SECRET so each test sets exactly what it needs."""
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    monkeypatch.delenv("JWT_SECRET",   raising=False)
    yield
    sys.modules.pop("auth.deps", None)


def test_dev_mode_imports_without_secret(_clean_env, monkeypatch):
    """AUTH_ENABLED=false (default) — module must import even with no secret set."""
    # No env vars set
    mod = _reimport_auth_deps()
    assert mod.AUTH_ENABLED is False
    # Default secret is fine in dev — no boot crash
    assert mod.SECRET_KEY == _DEFAULT_SECRET


def test_prod_with_real_secret_imports(_clean_env, monkeypatch):
    """AUTH_ENABLED=true + real JWT_SECRET — boots cleanly."""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_SECRET", "a" * 48)  # any non-default, non-empty value
    mod = _reimport_auth_deps()
    assert mod.AUTH_ENABLED is True
    assert mod.SECRET_KEY == "a" * 48


def test_prod_with_default_secret_refuses_to_boot(_clean_env, monkeypatch):
    """AUTH_ENABLED=true + default secret — RuntimeError at import time."""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_SECRET",   _DEFAULT_SECRET)
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        _reimport_auth_deps()


def test_prod_with_empty_secret_refuses_to_boot(_clean_env, monkeypatch):
    """AUTH_ENABLED=true + empty JWT_SECRET — RuntimeError at import time."""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_SECRET",   "")
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        _reimport_auth_deps()


def test_prod_without_secret_var_refuses_to_boot(_clean_env, monkeypatch):
    """AUTH_ENABLED=true + JWT_SECRET unset — falls back to default → RuntimeError."""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    # JWT_SECRET intentionally not set
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        _reimport_auth_deps()
