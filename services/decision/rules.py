"""Kural tabanlı tehdit değerlendirmesi — LLM'SİZ, deterministic.

Bu modül her zaman son sözü söyler. LLM advisor önerebilir, hiçbir zaman
override edemez. Savunma projelerinde bu zorunlu bir mimari seçim.

Tehdit skoru girdileri (weighted):
  - Korumalı bölge içinde mi?     +0.35
  - Transponder yok mu (unknown)? +0.15
  - Agresif hız (>30 m/s)?        +0.15
  - Agresif yön (bölgeye doğru)?  +0.15
  - Track güven skoru > 0.80?     +0.20 (baseline)
  = Max 1.00
"""
from __future__ import annotations

from services.decision.schemas import ThreatAssessment, ThreatLevel

AGGRESSIVE_SPEED_MPS = 30.0
HIGH_CONFIDENCE_THRESHOLD = 0.80

# Skor → seviye eşikleri
LEVEL_THRESHOLDS = {
    ThreatLevel.LOW: 0.0,
    ThreatLevel.MEDIUM: 0.30,
    ThreatLevel.HIGH: 0.60,
    ThreatLevel.CRITICAL: 0.85,
}


def _score_to_level(score: float) -> ThreatLevel:
    if score >= LEVEL_THRESHOLDS[ThreatLevel.CRITICAL]:
        return ThreatLevel.CRITICAL
    if score >= LEVEL_THRESHOLDS[ThreatLevel.HIGH]:
        return ThreatLevel.HIGH
    if score >= LEVEL_THRESHOLDS[ThreatLevel.MEDIUM]:
        return ThreatLevel.MEDIUM
    return ThreatLevel.LOW


def assess_threat(
    track: dict,
    inside_protected_zone: bool = False,
    heading_toward_zone: bool = False,
) -> ThreatAssessment:
    """Tek bir track için deterministic tehdit değerlendirmesi."""
    score = 0.0
    factors: list[str] = []

    has_transponder = bool(track.get("uas_id"))
    confidence = float(track.get("confidence", 0.0))
    vx = float(track.get("vx", 0.0))
    vy = float(track.get("vy", 0.0))
    speed = (vx * vx + vy * vy) ** 0.5

    confidence_high = confidence >= HIGH_CONFIDENCE_THRESHOLD
    aggressive_speed = speed >= AGGRESSIVE_SPEED_MPS
    unknown_transponder = not has_transponder

    if inside_protected_zone:
        score += 0.35
        factors.append("inside_protected_zone")
    if unknown_transponder:
        score += 0.15
        factors.append("unknown_transponder")
    if aggressive_speed:
        score += 0.15
        factors.append(f"aggressive_speed={speed:.1f}m/s")
    if heading_toward_zone:
        score += 0.15
        factors.append("heading_toward_zone")
    if confidence_high:
        score += 0.20
        factors.append(f"confidence={confidence:.2f}")

    score = min(score, 1.0)
    level = _score_to_level(score)

    reasoning = (
        f"score={score:.2f} level={level.value} factors=[{', '.join(factors) or 'none'}]"
    )

    return ThreatAssessment(
        track_id=track["track_id"],
        threat_level=level,
        score=score,
        reasoning=reasoning,
        inside_protected_zone=inside_protected_zone,
        unknown_transponder=unknown_transponder,
        aggressive_speed=aggressive_speed,
        aggressive_heading=heading_toward_zone,
        confidence_exceeds_threshold=confidence_high,
    )
