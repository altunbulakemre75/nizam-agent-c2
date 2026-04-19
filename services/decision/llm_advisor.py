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
    """Claude API (veya Ollama fallback) ile tehdit danışman sorgusu.

    Henüz paketler yoksa None döner — rule engine tek başına karar verir.
    """
    if not is_llm_enabled():
        return None
    try:
        import anthropic  # noqa: F401, PLC0415
    except ImportError:
        log.warning("anthropic paketi yok — LLM advisor atlanıyor")
        return None

    # TODO: gerçek LangGraph state machine entegrasyonu (Faz 6 tam).
    # Şu an placeholder — rule engine'in verdiği seviyeyi aynen onaylar.
    return LLMDecisionDict(
        threat_level=assessment.threat_level.value,
        action=Action.LOG.value,
        confidence=0.5,
        reasoning="LLM advisor placeholder — gerçek entegrasyon Faz 6 tam sürümünde.",
        roe_reference="",
    )


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
