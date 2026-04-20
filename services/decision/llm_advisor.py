"""LLM advisor — opsiyonel, kural engine'in YANINA çalışır, override etmez.

Aktivasyon: NIZAM_DECISION_LLM_ENABLED=true env değişkeni.
Paketler: pip install "langgraph>=0.2" "anthropic>=0.40" "llama-index>=0.12"

Davranış:
  1. Rule engine her zaman nihai karar kaynağı (safety-critical).
  2. LLM önerir, rule engine ile birleştirilir → reconcile().
  3. LLM "ENGAGE" dese bile, rule engine "ALERT" derse → ALERT kazanır.

Bu dosya SHELL — gerçek LangGraph implementasyonu paketler yüklenince
production-ready hale gelir. Şu an yoksa placeholder döner.
"""
from __future__ import annotations

import logging
import os
from typing import Literal, TypedDict

from services.decision.schemas import (
    Action,
    Decision,
    DecisionSource,
    ThreatAssessment,
    ThreatLevel,
)

log = logging.getLogger(__name__)

LLMDecisionDict = TypedDict(
    "LLMDecisionDict",
    {
        "threat_level": Literal["low", "medium", "high", "critical"],
        "action": Literal["log", "alert", "engage", "handoff"],
        "confidence": float,
        "reasoning": str,
        "roe_reference": str,
    },
    total=False,
)


def is_llm_enabled() -> bool:
    return os.getenv("NIZAM_DECISION_LLM_ENABLED", "false").lower() == "true"


async def query_llm_advisor(
    track: dict, assessment: ThreatAssessment
) -> LLMDecisionDict | None:
    """Claude API ile tehdit danışman sorgusu — structured output.

    Anthropic kurulu değilse None döner.
    ROE RAG entegrasyonu: varsa doktrin bağlamı prompt'a eklenir.
    """
    if not is_llm_enabled():
        return None
    try:
        from anthropic import AsyncAnthropic  # noqa: PLC0415
    except ImportError:
        log.warning("anthropic paketi yok — LLM advisor atlanıyor")
        return None

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("ANTHROPIC_API_KEY set değil — LLM advisor atlanıyor")
        return None

    # Opsiyonel: ROE RAG bağlamı
    roe_context = ""
    try:
        from services.knowledge.roe_rag import ROERAG  # noqa: PLC0415

        rag = ROERAG()
        roe_results = rag.query(
            f"threat level {assessment.threat_level.value} "
            f"{'inside zone' if assessment.inside_protected_zone else 'outside zone'}"
        )
        if roe_results:
            roe_context = "\n\nRelevant doctrine:\n" + "\n".join(
                f"- [{r.rule_id or r.source}] {r.excerpt}" for r in roe_results[:2]
            )
    except Exception as exc:
        log.debug("ROE RAG sorgusu başarısız: %s", exc)

    # Structured output — Claude tool use
    tools = [{
        "name": "submit_assessment",
        "description": "Submit threat assessment with ROE-compliant action recommendation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "threat_level": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                "action": {"type": "string", "enum": ["log", "alert", "handoff"]},  # ENGAGE YASAK
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reasoning": {"type": "string", "maxLength": 300},
                "roe_reference": {"type": "string"},
            },
            "required": ["threat_level", "action", "confidence", "reasoning"],
        },
    }]

    prompt = (
        f"Counter-UAS track assessment.\n"
        f"Track: id={track.get('track_id')} conf={track.get('confidence', 0):.2f}\n"
        f"Position: x={track.get('x', 0):.0f} y={track.get('y', 0):.0f} z={track.get('z', 0):.0f}\n"
        f"Velocity: vx={track.get('vx', 0):.1f} vy={track.get('vy', 0):.1f}\n"
        f"Sources: {track.get('sources', [])}\n"
        f"UAS ID: {track.get('uas_id') or 'unknown'}\n\n"
        f"Rule engine pre-assessment:\n"
        f"  threat_level={assessment.threat_level.value}\n"
        f"  score={assessment.score:.2f}\n"
        f"  reasoning={assessment.reasoning}\n"
        f"{roe_context}\n\n"
        "You are an advisor, not the decision maker. Submit your independent "
        "assessment via submit_assessment tool. NEVER recommend ENGAGE — only "
        "LOG, ALERT, or HANDOFF. Operators and rule engine control ENGAGE."
    )

    client = AsyncAnthropic(api_key=api_key)
    try:
        msg = await client.messages.create(
            model=os.getenv("NIZAM_LLM_MODEL", "claude-sonnet-4-6"),
            max_tokens=512,
            tools=tools,
            tool_choice={"type": "tool", "name": "submit_assessment"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        log.warning("Claude API çağrısı başarısız: %s", exc)
        return None

    for block in msg.content:
        if block.type == "tool_use" and block.name == "submit_assessment":
            return LLMDecisionDict(**block.input)
    return None


def reconcile(rule_decision: Decision, llm_hint: LLMDecisionDict | None) -> Decision:
    """LLM hint'i ile rule decision'ı birleştir — SAFETY FIRST.

    Kurallar:
      1. Rule engine ENGAGE demedi ise, LLM asla ENGAGE tetikleyemez.
      2. Rule engine ALERT demişse, LLM HANDOFF diyebilir (upgrade ok).
      3. Rule engine LOG demişse, LLM ALERT diyebilir (upgrade ok).
      4. Rule engine daha ciddi → LLM downgrade edemez.
      5. LLM reasoning rule decision reasoning'ine EK olarak iliştirilir.
    """
    if llm_hint is None:
        return rule_decision

    rule_action = rule_decision.action
    llm_action_str = llm_hint.get("action", "log")
    llm_action = Action(llm_action_str)

    # Severity sıralaması (düşükten yükseğe)
    severity = {
        Action.LOG: 0,
        Action.ALERT: 1,
        Action.HANDOFF: 2,
        Action.ENGAGE: 3,
    }

    # LLM sadece yukarı yönde upgrade öner, hiçbir zaman ENGAGE'e çıkaramaz
    final_action = rule_action
    if llm_action != Action.ENGAGE and severity[llm_action] > severity[rule_action]:
        final_action = llm_action

    merged_reasoning = f"{rule_decision.reasoning} | LLM: {llm_hint.get('reasoning', '')[:120]}"

    return Decision(
        track_id=rule_decision.track_id,
        action=final_action,
        threat_level=rule_decision.threat_level,  # rule engine seviyesi
        confidence=rule_decision.confidence,
        reasoning=merged_reasoning[:500],
        source=DecisionSource.RULE_ENGINE if final_action == rule_action else DecisionSource.LLM_ADVISOR,
        roe_reference=rule_decision.roe_reference,
        requires_operator_approval=rule_decision.requires_operator_approval or (final_action == Action.ENGAGE),
        timestamp_iso=rule_decision.timestamp_iso,
    )


# Boş kullanım önlemek için re-export
__all__ = ["query_llm_advisor", "reconcile", "is_llm_enabled", "ThreatLevel"]
