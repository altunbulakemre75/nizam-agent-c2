"""Guardrail safety tests."""
from __future__ import annotations

from datetime import datetime, timezone

from services.decision.guardrails import (
    FriendlyZone,
    apply_guardrails,
    civilian_pattern_guardrail,
    friendly_zone_guardrail,
    input_track_guardrail,
)
from services.decision.schemas import Action, Decision, DecisionSource, ThreatLevel


def _decision(action: Action) -> Decision:
    return Decision(
        track_id="t1", action=action, threat_level=ThreatLevel.HIGH,
        confidence=0.8, reasoning="rule says X", source=DecisionSource.RULE_ENGINE,
        roe_reference="ROE-X", requires_operator_approval=False,
        timestamp_iso=datetime.now(timezone.utc).isoformat(),
    )


# ── Input guardrail ───────────────────────────────────────────────

def test_input_low_confidence_triggers_log():
    result = input_track_guardrail({"confidence": 0.05, "hits": 5, "latitude": 39.9, "longitude": 32.8})
    assert result.triggered
    assert result.downgrade_to == Action.LOG


def test_input_single_tick_triggers_log():
    result = input_track_guardrail({"confidence": 0.9, "hits": 1, "latitude": 39.9, "longitude": 32.8})
    assert result.triggered


def test_input_zero_coords_triggers_log():
    result = input_track_guardrail({"confidence": 0.9, "hits": 5, "latitude": 0.0, "longitude": 0.0})
    assert result.triggered


def test_input_healthy_track_passes():
    result = input_track_guardrail({"confidence": 0.9, "hits": 5, "latitude": 39.9, "longitude": 32.8})
    assert not result.triggered


# ── Friendly zone guardrail ───────────────────────────────────────

ANKARA_ZONE = FriendlyZone(
    zone_id="OP-01", name="ops center",
    center_lat=39.9334, center_lon=32.8597, radius_m=500,
)


def test_friendly_zone_inside_triggers_alert():
    result = friendly_zone_guardrail(
        {"latitude": 39.9335, "longitude": 32.8598},
        zones=[ANKARA_ZONE],
    )
    assert result.triggered
    assert result.downgrade_to == Action.ALERT


def test_friendly_zone_outside_passes():
    result = friendly_zone_guardrail(
        {"latitude": 40.0, "longitude": 33.0},  # çok uzakta
        zones=[ANKARA_ZONE],
    )
    assert not result.triggered


def test_friendly_zone_empty_list_passes():
    result = friendly_zone_guardrail({"latitude": 39.9334, "longitude": 32.8597}, zones=[])
    assert not result.triggered


# ── Civilian pattern guardrail ────────────────────────────────────

def test_civilian_transponder_triggers_alert():
    track = {"uas_id": "TC-ABC123", "vx": 10, "vy": 5, "altitude": 500}
    result = civilian_pattern_guardrail(track)
    assert result.triggered
    assert result.downgrade_to == Action.ALERT


def test_fast_high_pattern_triggers_alert():
    track = {"vx": 150, "vy": 100, "altitude": 5000}
    result = civilian_pattern_guardrail(track)
    assert result.triggered


def test_slow_low_pattern_passes():
    track = {"vx": 20, "vy": 0, "altitude": 100}
    result = civilian_pattern_guardrail(track)
    assert not result.triggered


# ── Orkestrator integration ───────────────────────────────────────

def test_apply_guardrails_downgrades_engage_to_alert_in_friendly_zone():
    engage = _decision(Action.ENGAGE)
    track = {"latitude": 39.9335, "longitude": 32.8598,  # zone inside
             "confidence": 0.9, "hits": 10, "vx": 5, "vy": 0, "altitude": 100}
    result = apply_guardrails(engage, track, friendly_zones=[ANKARA_ZONE])
    assert result.action == Action.ALERT
    assert "friendly-zone-OP-01" in result.guardrails_triggered


def test_apply_guardrails_no_upgrade():
    """Guardrail'ler ASLA LOG'dan HIGH'a çıkarmamalı."""
    log_decision = _decision(Action.LOG)
    result = apply_guardrails(log_decision, {"confidence": 0.9, "hits": 5, "latitude": 39.9, "longitude": 32.8})
    # LOG zaten en düşük severity; guardrails tetiklenmez veya LOG'da kalır
    assert result.action == Action.LOG


def test_apply_guardrails_preserves_reasoning_separate_field():
    """Guardrail açıklaması ayrı field — reasoning dokunulmaz."""
    engage = _decision(Action.ENGAGE)
    track = {"confidence": 0.03, "hits": 1, "latitude": 39.9, "longitude": 32.8}
    result = apply_guardrails(engage, track)
    # Orijinal reasoning aynen korundu, kırpma yok
    assert result.reasoning == "rule says X"
    # Guardrail açıklaması guardrail_reasoning field'ında
    assert result.guardrail_reasoning


def test_apply_guardrails_civilian_pattern_downgrades_engage():
    """ENGAGE + sivil uçak deseni → ALERT'e düşer."""
    engage = _decision(Action.ENGAGE)
    track = {"confidence": 0.9, "hits": 10, "latitude": 50.0, "longitude": 30.0,
             "vx": 200, "vy": 0, "altitude": 10000}  # airliner pattern
    result = apply_guardrails(engage, track, friendly_zones=[])
    assert result.action == Action.ALERT
    assert "civilian-airliner-pattern" in result.guardrails_triggered
