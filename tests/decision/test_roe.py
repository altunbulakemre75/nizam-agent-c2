"""ROE loader + evaluator tests — SAFETY CRITICAL."""
from __future__ import annotations

from pathlib import Path

from services.decision.roe import evaluate_roe, load_roe
from services.decision.schemas import Action, ROERule, ThreatLevel


CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "roe" / "default.yaml"


# ── Loader tests ──────────────────────────────────────────────────

def test_default_roe_loads():
    rules = load_roe(CONFIG_PATH)
    assert len(rules) >= 5
    assert all(isinstance(r, ROERule) for r in rules)


def test_default_engage_rule_is_DISABLED():
    """CRITICAL: ENGAGE kuralı varsayılan olarak DEVRE DIŞI olmalı."""
    rules = load_roe(CONFIG_PATH)
    engage_rules = [r for r in rules if r.action == Action.ENGAGE]
    # En az bir ENGAGE kuralı var ve hepsi disabled
    assert len(engage_rules) >= 1
    for rule in engage_rules:
        assert rule.enabled is False, (
            f"ENGAGE rule {rule.rule_id} is ENABLED by default — güvenlik ihlali!"
        )


def test_default_engage_always_requires_operator_approval():
    rules = load_roe(CONFIG_PATH)
    for rule in rules:
        if rule.action == Action.ENGAGE:
            assert rule.requires_operator_approval is True


# ── Evaluator tests ───────────────────────────────────────────────

def test_no_rules_defaults_to_log():
    action, matched = evaluate_roe([], ThreatLevel.CRITICAL, inside_zone=True)
    assert action == Action.LOG
    assert matched is None


def test_first_matching_rule_wins():
    rules = [
        ROERule(
            rule_id="R1", description="first", when_threat_level=ThreatLevel.HIGH,
            action=Action.ALERT, enabled=True,
        ),
        ROERule(
            rule_id="R2", description="second (never reached)",
            when_threat_level=ThreatLevel.HIGH, action=Action.HANDOFF, enabled=True,
        ),
    ]
    action, matched = evaluate_roe(rules, ThreatLevel.HIGH, inside_zone=False)
    assert action == Action.ALERT
    assert matched is not None
    assert matched.rule_id == "R1"


def test_disabled_rule_is_skipped():
    rules = [
        ROERule(
            rule_id="DISABLED", description="x", when_threat_level=ThreatLevel.HIGH,
            action=Action.ENGAGE, enabled=False,
        ),
        ROERule(
            rule_id="ENABLED", description="y", when_threat_level=ThreatLevel.HIGH,
            action=Action.ALERT, enabled=True,
        ),
    ]
    action, matched = evaluate_roe(rules, ThreatLevel.HIGH, inside_zone=False)
    assert action == Action.ALERT
    assert matched is not None
    assert matched.rule_id == "ENABLED"


def test_zone_constraint_must_match():
    rules = [
        ROERule(
            rule_id="IN_ZONE", description="zone-only",
            when_threat_level=ThreatLevel.HIGH, when_inside_zone=True,
            action=Action.HANDOFF, enabled=True,
        ),
    ]
    # Zone dışında → eşleşmez
    action_out, _ = evaluate_roe(rules, ThreatLevel.HIGH, inside_zone=False)
    assert action_out == Action.LOG

    # Zone içinde → eşleşir
    action_in, matched = evaluate_roe(rules, ThreatLevel.HIGH, inside_zone=True)
    assert action_in == Action.HANDOFF
    assert matched is not None
    assert matched.rule_id == "IN_ZONE"
