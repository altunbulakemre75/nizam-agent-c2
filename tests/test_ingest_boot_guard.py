"""
tests/test_ingest_boot_guard.py — Boot-time INGEST_API_KEY guard.

Pins the contract that cop/routers/ingest.py refuses to import when
AUTH_ENABLED=true and INGEST_API_KEY is missing. Without this guard the
old runtime check (`if AUTH_ENABLED and INGEST_API_KEY`) silently failed
open: empty key meant no key check, every unauthenticated request
accepted. Same fail-closed pattern as test_auth_boot_guard.py for
JWT_SECRET.
"""
from __future__ import annotations

import importlib
import sys

import pytest


def _reimport_chain():
    """
    Force re-import of the chain auth.deps → cop.routers.ingest so the
    module-level boot guards re-run with the current environment.
    """
    for mod in ("cop.routers.ingest", "auth.deps"):
        sys.modules.pop(mod, None)
    # Importing ingest pulls auth.deps transitively.
    return importlib.import_module("cop.routers.ingest")


@pytest.fixture
def _clean_env(monkeypatch):
    monkeypatch.delenv("AUTH_ENABLED",   raising=False)
    monkeypatch.delenv("JWT_SECRET",     raising=False)
    monkeypatch.delenv("INGEST_API_KEY", raising=False)
    yield
    for mod in ("cop.routers.ingest", "auth.deps"):
        sys.modules.pop(mod, None)


def test_dev_mode_imports_without_ingest_key(_clean_env):
    """AUTH_ENABLED=false (default) — no key needed, module imports cleanly."""
    mod = _reimport_chain()
    assert mod.AUTH_ENABLED is False
    assert mod.INGEST_API_KEY == ""


def test_prod_with_real_key_imports(_clean_env, monkeypatch):
    """AUTH_ENABLED=true + JWT_SECRET + INGEST_API_KEY all set → boots cleanly."""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_SECRET",   "x" * 48)
    monkeypatch.setenv("INGEST_API_KEY", "k" * 32)
    mod = _reimport_chain()
    assert mod.AUTH_ENABLED is True
    assert mod.INGEST_API_KEY == "k" * 32


def test_prod_without_ingest_key_refuses_to_boot(_clean_env, monkeypatch):
    """AUTH_ENABLED=true but INGEST_API_KEY unset → RuntimeError at import time."""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_SECRET",   "x" * 48)
    # INGEST_API_KEY intentionally not set
    with pytest.raises(RuntimeError, match="INGEST_API_KEY"):
        _reimport_chain()


def test_prod_with_empty_ingest_key_refuses_to_boot(_clean_env, monkeypatch):
    """AUTH_ENABLED=true + INGEST_API_KEY="" → RuntimeError at import time."""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_SECRET",   "x" * 48)
    monkeypatch.setenv("INGEST_API_KEY", "")
    with pytest.raises(RuntimeError, match="INGEST_API_KEY"):
        _reimport_chain()
