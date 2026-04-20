"""LangGraph 5-node state machine tests (fallback path, LLM disabled)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from services.decision.guardrails import FriendlyZone
from services.decision.llm_graph import run_graph
from services.decision.roe import load_roe
from services.decision.schemas import Action, ThreatLevel


CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "roe" / "default.yaml"


def _track(**overrides) -> dict:
    base = {
        "track_id": "t-graph",
        "latitude": 40.0, "longitude": 33.0,   # zone dışı
        "altitude": 100.0,
        "confidence": 0.9,
        "hits": 10,
        "vx": 0.0, "vy": 0.0, "vz": 0.0,
        "sources": ["camera"],
        "uas_id": None,
        "class_name": None,
        "x": 0.0, "y": 0.0, "z": 100.0,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_graph_runs_without_llm():
    rules = load_roe(CONFIG_PATH)
    decision = await run_graph(_track(), rules)
    assert decision is not None
    assert decision.track_id == "t-graph"
    # LLM devre dışı → rule engine kararı
    assert decision.source.value in ("rule_engine", "llm_advisor")


@pytest.mark.asyncio
async def test_graph_triggers_guardrail_in_friendly_zone():
    rules = load_roe(CONFIG_PATH)
    zones = [FriendlyZone(
        zone_id="OP", name="ops",
        center_lat=39.9334, center_lon=32.8597, radius_m=500,
    )]
    track = _track(latitude=39.9335, longitude=32.8598, confidence=0.9)  # zone içi
    decision = await run_graph(track, rules, friendly_zones=zones,
                               inside_protected_zone=True)
    # Zone içindeki HIGH → HANDOFF olabilir ama friendly zone alırsa ALERT
    assert decision.action != Action.ENGAGE
    # friendly-zone guardrail tetiklendi
    assert any("friendly-zone" in g for g in decision.guardrails_triggered)


@pytest.mark.asyncio
async def test_graph_audit_trail_present():
    rules = load_roe(CONFIG_PATH)
    decision = await run_graph(_track(), rules)
    assert decision.timestamp_iso
    assert isinstance(decision.guardrails_triggered, list)
