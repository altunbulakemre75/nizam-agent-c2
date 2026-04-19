"""LLM advisor + reconciliation tests — SAFETY CRITICAL."""
from __future__ import annotations

from datetime import datetime, timezone

from services.decision.llm_advisor import reconcile
from services.decision.schemas import Action, Decision, DecisionSource, ThreatLevel


def _rule_decision(action: Action) -> Decision:
    return Decision(
        track_id="t1", action=action, threat_level=ThreatLevel.HIGH,
        confidence=0.8, reasoning="rule reasoning",
        source=DecisionSource.RULE_ENGINE,
        roe_reference="ROE-X", requires_operator_approval=False,
        timestamp_iso=datetime.now(timezone.utc).isoformat(),
    )


def test_reconcile_no_llm_hint_returns_rule_unchanged():
    rule = _rule_decision(Action.ALERT)
    result = reconcile(rule, None)
    assert result is rule


def test_llm_cannot_upgrade_to_engage():
    rule = _rule_decision(Action.ALERT)
    llm = {
        "threat_level": "critical", "action": "engage",
        "confidence": 0.95, "reasoning": "LLM says engage", "roe_reference": "",
    }
    result = reconcile(rule, llm)
    assert result.action != Action.ENGAGE
    assert result.action == Action.ALERT


def test_llm_can_upgrade_log_to_alert():
    rule = _rule_decision(Action.LOG)
    llm = {
        "threat_level": "medium", "action": "alert",
        "confidence": 0.7, "reasoning": "aggressive pattern", "roe_reference": "",
    }
    result = reconcile(rule, llm)
    assert result.action == Action.ALERT


def test_llm_cannot_downgrade_alert_to_log():
    rule = _rule_decision(Action.ALERT)
    llm = {
        "threat_level": "low", "action": "log",
        "confidence": 0.6, "reasoning": "false positive", "roe_reference": "",
    }
    result = reconcile(rule, llm)
    assert result.action == Action.ALERT   # rule kazanır


def test_llm_reasoning_appended_to_decision():
    rule = _rule_decision(Action.ALERT)
    llm = {
        "threat_level": "high", "action": "handoff",
        "confidence": 0.9, "reasoning": "escalation pattern", "roe_reference": "",
    }
    result = reconcile(rule, llm)
    assert "LLM:" in result.reasoning
    assert "escalation pattern" in result.reasoning
