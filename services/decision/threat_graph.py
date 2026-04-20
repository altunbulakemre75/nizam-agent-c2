"""Karar katmanı entry-point'leri.

Tek production path: ``llm_graph.run_graph()`` — async, 5-node pipeline
(rule → RAG → LLM → guardrail → checkpoint). Bu modül iki shim sağlar:

  - ``decide(track, rules, ...)`` — sync, RULE-ONLY fast path (LLM kapalı).
    Test ve CLI çağrıları için. ``apply_guards=True`` ile guardrails de
    ekleyebilir ama LLM advisor çalıştırmaz.

  - ``decide_full(track, rules, ...)`` — sync wrapper over ``run_graph``.
    Full production pipeline (LLM + RAG + guardrail + checkpoint).
    Event loop'un içindeysen doğrudan ``await run_graph()`` çağır.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from services.decision.guardrails import FriendlyZone, apply_guardrails
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
    friendly_zones: list[FriendlyZone] | None = None,
    apply_guards: bool = False,
) -> tuple[ThreatAssessment, Decision]:
    """Sync rule-only karar — LLM advisor ÇALIŞTIRMAZ.

    Full production pipeline için ``decide_full()`` veya
    ``await llm_graph.run_graph()``.
    """
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

    if action == Action.ENGAGE:
        approval = True  # safety hardening — kural yazarı unutsa bile

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

    if apply_guards:
        decision = apply_guardrails(decision, track, friendly_zones=friendly_zones)

    return assessment, decision


def decide_full(
    track: dict,
    roe_rules: list[ROERule],
    inside_protected_zone: bool = False,
    heading_toward_zone: bool = False,
    friendly_zones: list[FriendlyZone] | None = None,
) -> Decision:
    """Sync wrapper over full LangGraph 5-node production pipeline.

    ``asyncio.run()`` ile çalışır; mevcut bir event loop içindeysen
    doğrudan ``await llm_graph.run_graph(...)`` kullan.
    """
    from services.decision.llm_graph import run_graph

    return asyncio.run(run_graph(
        track, roe_rules,
        friendly_zones=friendly_zones,
        inside_protected_zone=inside_protected_zone,
        heading_toward_zone=heading_toward_zone,
    ))
