"""Karar katmanı şemaları — SGLang-style constrained output uyumlu.

Tüm kararlar ENUM ile sınırlandırılmış — LLM hallucination'ı engelleyemez
ama schema doğrulamasıyla fail-safe sağlar. Rule engine her zaman
son söz sahibi.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ThreatLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Action(str, Enum):
    LOG = "log"                # sadece kayda al
    ALERT = "alert"            # operatörü uyar
    ENGAGE = "engage"          # karşı önlem başlat (insan onayı sonrası)
    HANDOFF = "handoff"        # başka bir sisteme/operatöre devret


class DecisionSource(str, Enum):
    RULE_ENGINE = "rule_engine"
    LLM_ADVISOR = "llm_advisor"
    OPERATOR = "operator"


class ThreatAssessment(BaseModel):
    """Füzyon track'inin tehdit değerlendirmesi.

    Girdi tabanlı; LLM değil, deterministic skor üretiyor.
    """
    track_id: str
    threat_level: ThreatLevel
    score: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=500)

    # Tetikleyici faktörler (audit trail için)
    inside_protected_zone: bool = False
    unknown_transponder: bool = False
    aggressive_speed: bool = False
    aggressive_heading: bool = False
    confidence_exceeds_threshold: bool = False


class Decision(BaseModel):
    """Nihai aksiyon kararı — rule engine'den çıkar."""
    track_id: str
    action: Action
    threat_level: ThreatLevel
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=500)
    source: DecisionSource
    roe_reference: str | None = None  # hangi ROE kuralı tetikledi
    requires_operator_approval: bool = True  # ENGAGE için varsayılan True
    timestamp_iso: str

    # Audit trail — LLM output'u kısaltılmadan sakla
    llm_raw_response: dict | None = None      # Claude tool_use input (ham)
    llm_provider: str | None = None           # "anthropic" | "ollama"
    llm_model: str | None = None              # model adı
    guardrails_triggered: list[str] = Field(default_factory=list)
    guardrail_reasoning: str = ""             # guardrail açıklamaları (reasoning'e kırpılmaz)


class ROERule(BaseModel):
    """Tek bir ROE (Rules of Engagement) kuralı."""
    rule_id: str
    description: str
    when_threat_level: ThreatLevel
    when_inside_zone: bool | None = None  # None = önemli değil
    requires_operator_approval: bool = True
    action: Action
    enabled: bool = True
