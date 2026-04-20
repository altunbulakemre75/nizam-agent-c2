"""LLM client tests — Anthropic/Ollama fallback."""
from __future__ import annotations

import pytest

from services.decision.llm_client import DECISION_SCHEMA, LLMResponse


def test_schema_action_has_no_engage():
    """SAFETY: Claude'a ENGAGE seçimi verilmemeli."""
    assert "log" in DECISION_SCHEMA["properties"]["action"]["enum"]
    assert "alert" in DECISION_SCHEMA["properties"]["action"]["enum"]
    assert "handoff" in DECISION_SCHEMA["properties"]["action"]["enum"]
    assert "engage" not in DECISION_SCHEMA["properties"]["action"]["enum"]


def test_schema_required_fields():
    req = set(DECISION_SCHEMA["required"])
    assert {"threat_level", "action", "confidence", "reasoning"}.issubset(req)


def test_llm_response_dataclass_structure():
    r = LLMResponse(
        action="alert", threat_level="high", confidence=0.9,
        reasoning="test", roe_reference="ROE-3",
        raw={"x": 1}, provider="ollama", model="llama3",
    )
    assert r.action == "alert"
    assert r.raw == {"x": 1}


@pytest.mark.asyncio
async def test_query_llm_returns_none_when_no_provider(monkeypatch):
    """Ne Anthropic ne Ollama varsa → None."""
    from services.decision import llm_client

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Ollama fail et
    monkeypatch.setattr(llm_client, "OLLAMA_URL", "http://localhost:1")  # yanıt vermez
    result = await llm_client.query_llm("test prompt")
    assert result is None
