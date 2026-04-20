"""LLM sağlayıcı soyutlaması — Anthropic Claude öncelikli, Ollama fallback.

Saha dağıtımında internet olmayabilir. Sıralı olarak:
  1. ANTHROPIC_API_KEY varsa → Claude API
  2. Ollama localhost:11434 açıksa → local llama3.1
  3. İkisi de yoksa → None (LLM advisor tamamen devre dışı)

Her iki provider de aynı interface döndürür:
    LLMResponse(action, threat_level, confidence, reasoning, roe_reference, raw)
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")


@dataclass
class LLMResponse:
    action: str          # "log" | "alert" | "handoff"  (ENGAGE yasak)
    threat_level: str    # "low" | "medium" | "high" | "critical"
    confidence: float
    reasoning: str
    roe_reference: str | None
    raw: dict[str, Any]  # ham response — audit trail için
    provider: str        # "anthropic" | "ollama"
    model: str


# ── Schema tanımı (Claude tool + Ollama JSON schema) ──────────────

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "threat_level": {
            "type": "string",
            "enum": ["low", "medium", "high", "critical"],
        },
        "action": {
            "type": "string",
            "enum": ["log", "alert", "handoff"],   # ENGAGE LLM'e yasak
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reasoning": {"type": "string", "maxLength": 500},
        "roe_reference": {"type": "string"},
    },
    "required": ["threat_level", "action", "confidence", "reasoning"],
}


async def _try_anthropic(prompt: str) -> LLMResponse | None:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        log.debug("anthropic paketi yok")
        return None

    client = AsyncAnthropic(api_key=api_key)
    model = os.getenv("NIZAM_LLM_MODEL", "claude-sonnet-4-6")
    tools = [{
        "name": "submit_assessment",
        "description": "Submit counter-UAS threat assessment.",
        "input_schema": DECISION_SCHEMA,
    }]

    try:
        msg = await client.messages.create(
            model=model, max_tokens=512, tools=tools,
            tool_choice={"type": "tool", "name": "submit_assessment"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        log.warning("Claude API hata: %s", exc)
        return None

    for block in msg.content:
        if block.type == "tool_use" and block.name == "submit_assessment":
            d = dict(block.input)
            return LLMResponse(
                action=d["action"], threat_level=d["threat_level"],
                confidence=float(d["confidence"]), reasoning=d["reasoning"],
                roe_reference=d.get("roe_reference"),
                raw=d, provider="anthropic", model=model,
            )
    return None


async def _try_ollama(prompt: str) -> LLMResponse | None:
    """Ollama /api/generate endpoint — JSON format=json ile structured output."""
    full_prompt = (
        prompt + "\n\nRespond ONLY with JSON matching this schema:\n"
        + json.dumps(DECISION_SCHEMA, indent=2)
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": OLLAMA_MODEL, "prompt": full_prompt, "format": "json", "stream": False},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        log.debug("Ollama yok veya hata: %s", exc)
        return None

    response_text = data.get("response", "")
    try:
        parsed = json.loads(response_text)
    except json.JSONDecodeError:
        log.warning("Ollama JSON parse edilemedi: %s", response_text[:200])
        return None

    # Şema doğrulama (LLM bazen enum dışı çıkar)
    action = parsed.get("action", "log")
    if action not in ("log", "alert", "handoff"):
        log.warning("Ollama geçersiz action döndü: %s — 'log'a düşürülüyor", action)
        action = "log"

    return LLMResponse(
        action=action,
        threat_level=parsed.get("threat_level", "low"),
        confidence=float(parsed.get("confidence", 0.5)),
        reasoning=str(parsed.get("reasoning", ""))[:500],
        roe_reference=parsed.get("roe_reference"),
        raw=parsed, provider="ollama", model=OLLAMA_MODEL,
    )


async def query_llm(prompt: str) -> LLMResponse | None:
    """Provider fallback: Anthropic → Ollama → None."""
    response = await _try_anthropic(prompt)
    if response is not None:
        return response
    response = await _try_ollama(prompt)
    if response is not None:
        return response
    return None
