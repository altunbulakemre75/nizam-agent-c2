"""
tests/test_registry.py  —  AI analyzer registry contract

Locks the Week 4 refactor's extensibility contract: you can register a
new analyzer, it runs in declaration order, failures are isolated, and
FakeClock drives the elapsed_ms timing so the numbers are reproducible.
"""
from __future__ import annotations

import pytest

from ai import registry as ai_registry
from ai.registry import Analyzer, register, run_all, list_analyzers, clear
from shared.clock import FakeClock, set_clock, reset_clock


@pytest.fixture(autouse=True)
def _clean_registry():
    clear()
    yield
    clear()


@pytest.fixture
def fake_clock():
    clock = FakeClock(start=1_700_000_000.0, monotonic_start=0.0)
    set_clock(clock)
    yield clock
    reset_clock()


def test_register_and_list():
    register(Analyzer(name="alpha", fn=lambda: "a"))
    register(Analyzer(name="beta",  fn=lambda: "b"))
    names = [a.name for a in list_analyzers()]
    assert names == ["alpha", "beta"]


def test_register_is_idempotent_by_name():
    """Re-registering the same name replaces the old entry, not duplicate."""
    register(Analyzer(name="alpha", fn=lambda: "a1"))
    register(Analyzer(name="alpha", fn=lambda: "a2"))
    results = run_all()
    assert results["alpha"]["result"] == "a2"
    assert len(list_analyzers()) == 1


def test_run_all_honours_stage_filter():
    register(Analyzer(name="tac",    fn=lambda: 1, stage="tactical.analyze"))
    register(Analyzer(name="periodic", fn=lambda: 2, stage="periodic.hourly"))

    tac_out = run_all(stage="tactical.analyze")
    assert list(tac_out.keys()) == ["tac"]

    per_out = run_all(stage="periodic.hourly")
    assert list(per_out.keys()) == ["periodic"]


def test_failures_are_isolated(fake_clock):
    """One exploding analyzer must not take down the others."""
    def explode():
        raise RuntimeError("boom")

    register(Analyzer(name="ok_before", fn=lambda: "ok"))
    register(Analyzer(name="exploder",  fn=explode))
    register(Analyzer(name="ok_after",  fn=lambda: "still_ok"))

    out = run_all()
    assert out["ok_before"]["result"] == "ok"
    assert "error" in out["exploder"]
    assert "RuntimeError: boom" in out["exploder"]["error"]
    assert out["ok_after"]["result"] == "still_ok"


def test_result_key_overrides_name():
    register(Analyzer(name="compute_clusters", fn=lambda: [1, 2, 3], result_key="clusters"))
    out = run_all()
    assert "clusters" in out
    assert "compute_clusters" not in out
    assert out["clusters"]["result"] == [1, 2, 3]


def test_elapsed_ms_reflects_fake_clock(fake_clock):
    """FakeClock.advance inside the analyzer fn should produce bit-exact elapsed_ms."""
    def slow():
        fake_clock.advance(0.25)  # 250 ms of fake time
        return "done"

    register(Analyzer(name="slow_one", fn=slow))
    out = run_all()
    assert out["slow_one"]["elapsed_ms"] == 250.0


def test_unregister():
    register(Analyzer(name="alpha", fn=lambda: "a"))
    register(Analyzer(name="beta",  fn=lambda: "b"))
    assert ai_registry.unregister("alpha") is True
    assert ai_registry.unregister("alpha") is False  # already gone
    assert [a.name for a in list_analyzers()] == ["beta"]
