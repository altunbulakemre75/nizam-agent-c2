"""Karar state machine — rule engine + (opsiyonel) LLM advisor.

Akış:
    Track → assess_threat()  → ThreatAssessment
          → evaluate_roe()   → Action + matched rule
          → Decision (operatör onayı gerekiyorsa requires_operator_approval=True)

LangGraph entegrasyonu opsiyonel; deterministic path bu dosyadadır ve
her zaman son söz sahibidir.
"""
from __future__ import annotations

from datetime import datetime, timezone

from services.decision.roe import evaluate_roe
from services.decision.rules import assess_threat
from services.decision.schemas import (
    Action,
    Decision,
    DecisionSource,
    ROERule,
    ThreatAssessment,
)


def decide(
    track: dict,
    roe_rules: list[ROERule],
    inside_protected_zone: bool = False,
    heading_toward_zone: bool = False,
) -> tuple[ThreatAssessment, Decision]:
    """Tek track için tehdit değerlendir + ROE uygula → Decision."""
    assessment = assess_threat(
        track,
        inside_protected_zone=inside_protected_zone,
        heading_toward_zone=heading_toward_zone,
    )
    action, matched = evaluate_roe(roe_rules, assessment.threat_level, inside_protected_zone)

    if matched is not None:
        approval = matched.requires_operator_approval
        rule_ref: str | None = matched.rule_id
    else:
        approval = False
        rule_ref = None

    # ENGAGE her durumda operatör onayı gerekli — override override'ı yok
    if action == Action.ENGAGE:
        approval = True

    decision = Decision(
        track_id=track["track_id"],
        action=action,
        threat_level=assessment.threat_level,
        confidence=float(track.get("confidence", 0.0)),
        reasoning=assessment.reasoning,
        source=DecisionSource.RULE_ENGINE,
        roe_reference=rule_ref,
        requires_operator_approval=approval,
        timestamp_iso=datetime.now(timezone.utc).isoformat(),
    )
    return assessment, decision
