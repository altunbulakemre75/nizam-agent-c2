"""Deterministic tehdit değerlendirme testleri."""
from __future__ import annotations

from services.decision.rules import assess_threat
from services.decision.schemas import ThreatLevel


def _track(**overrides) -> dict:
    base = {
        "track_id": "t1",
        "confidence": 0.5,
        "vx": 0.0, "vy": 0.0, "vz": 0.0,
    }
    base.update(overrides)
    return base


def test_benign_unknown_track_is_low():
    result = assess_threat(_track())
    assert result.threat_level == ThreatLevel.LOW


def test_high_confidence_alone_is_medium():
    result = assess_threat(_track(confidence=0.9))
    # 0.20 (conf) + 0.15 (unknown_transponder) = 0.35 → MEDIUM
    assert result.threat_level == ThreatLevel.MEDIUM


def test_inside_protected_zone_aggressive_heading_high():
    result = assess_threat(
        _track(confidence=0.9), inside_protected_zone=True, heading_toward_zone=True
    )
    # 0.35 + 0.15 + 0.15 + 0.20 = 0.85 → CRITICAL
    assert result.threat_level == ThreatLevel.CRITICAL


def test_known_transponder_lowers_score():
    with_id = assess_threat(_track(uas_id="DJI-REGISTERED-123", confidence=0.9))
    without_id = assess_threat(_track(confidence=0.9))
    assert with_id.score < without_id.score


def test_aggressive_speed_triggers_factor():
    result = assess_threat(_track(vx=40.0, vy=0.0))
    assert result.aggressive_speed is True
    assert "aggressive_speed" in result.reasoning


def test_speed_below_threshold_not_aggressive():
    result = assess_threat(_track(vx=10.0, vy=0.0))
    assert result.aggressive_speed is False


def test_score_capped_at_one():
    result = assess_threat(
        _track(confidence=1.0, vx=100.0),
        inside_protected_zone=True, heading_toward_zone=True,
    )
    assert result.score == 1.0
    assert result.threat_level == ThreatLevel.CRITICAL


def test_reasoning_lists_contributing_factors():
    result = assess_threat(
        _track(confidence=0.9), inside_protected_zone=True, heading_toward_zone=True
    )
    assert "inside_protected_zone" in result.reasoning
    assert "heading_toward_zone" in result.reasoning
