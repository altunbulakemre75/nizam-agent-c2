"""
ai/llm_advisor.py  —  LLM-powered tactical advisor (Claude / OpenAI / Ollama)

Provides:
  - Situation briefing: natural-language summary of current COP state
  - Operator chat: answer questions about the tactical situation
  - Command parsing: translate natural-language orders into API calls

Supports Claude (Anthropic), OpenAI, and Ollama (local, free) via httpx.
Falls back to a rule-based summarizer when no provider is configured.

ENV:
  LLM_PROVIDER        anthropic | openai | ollama  (default: anthropic)
  ANTHROPIC_API_KEY    sk-ant-...
  OPENAI_API_KEY       sk-...
  OLLAMA_URL           http://localhost:11434  (default)
  LLM_MODEL            model id override
                         anthropic default: claude-sonnet-4-20250514
                         openai default:    gpt-4o
                         ollama default:    llama3.2
"""
from __future__ import annotations

import json
import os
import logging
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger("nizam.ai.llm")

# ── Configuration ───────────────────────────────────────────────────────────

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic").lower()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

LLM_MODEL = os.environ.get("LLM_MODEL", "")

LLM_ENABLED = bool(
    (LLM_PROVIDER == "ollama") or
    ANTHROPIC_API_KEY or
    OPENAI_API_KEY
)

# System prompt for the tactical advisor
SYSTEM_PROMPT = """Sen NIZAM COP sisteminin taktik danismanisin. Gorevlerin:
1. Operator sorularina durumsal farkindalik temelinde yanit ver
2. Mevcut tehdit durumunu analiz et
3. Taktik oneriler sun
4. Komutlari yorumla

KURALLAR:
- Yanit ver: Turkce, kisa ve net
- Askeri terminoloji kullan ama anlasilir ol
- Tehdit seviyelerini vurgula (HIGH/MEDIUM/LOW)
- Eger saldiri tespit edilmisse acil uyar
- Somut, uygulanabilir oneriler ver
- Sensor verilerine dayanarak konuş, tahmin etme"""


# ── State snapshot builder ──────────────────────────────────────────────────

def _build_context(
    tracks: Dict[str, Dict],
    threats: Dict[str, Dict],
    assets: Dict[str, Dict],
    zones: Dict[str, Dict],
    anomalies: List[Dict],
    recommendations: List[Dict],
) -> str:
    """Build a concise text summary of current COP state for LLM context."""
    lines = ["=== NIZAM COP DURUM RAPORU ===", ""]

    # Tracks summary
    high_threats = [t for t in threats.values() if t.get("threat_level") == "HIGH"]
    med_threats = [t for t in threats.values() if t.get("threat_level") == "MEDIUM"]
    lines.append(f"IZLER: {len(tracks)} aktif iz, "
                 f"{len(high_threats)} YUKSEK tehdit, "
                 f"{len(med_threats)} ORTA tehdit")

    for tid, threat in threats.items():
        level = threat.get("threat_level", "?")
        intent = threat.get("intent", "?")
        score = threat.get("score", "?")
        tti = threat.get("tti_s")
        track = tracks.get(tid, {})
        lat = track.get("lat", "?")
        lon = track.get("lon", "?")
        tti_str = f", TTI:{tti}s" if tti else ""
        lines.append(f"  - {tid}: {level} | {intent} | skor:{score}{tti_str} "
                     f"| konum:({lat},{lon})")

    # Assets
    friendlies = [a for a in assets.values() if a.get("type") == "friendly"]
    hostiles = [a for a in assets.values() if a.get("type") == "hostile"]
    lines.append(f"\nVARLIKLAR: {len(friendlies)} dost, {len(hostiles)} dusman, "
                 f"{len(assets) - len(friendlies) - len(hostiles)} bilinmeyen")
    for a in friendlies:
        lines.append(f"  - [DOST] {a.get('name', a['id'])}: ({a['lat']},{a['lon']})")
    for a in hostiles:
        lines.append(f"  - [DUSMAN] {a.get('name', a['id'])}: ({a['lat']},{a['lon']})")

    # Zones
    lines.append(f"\nBOLGELER: {len(zones)} tanimli")
    for z in zones.values():
        lines.append(f"  - {z.get('name', z['id'])}: {z.get('type', '?')}")

    # Anomalies
    if anomalies:
        lines.append(f"\nANOMALILER: {len(anomalies)} tespit")
        for a in anomalies[:5]:
            lines.append(f"  - [{a.get('severity','?')}] {a.get('type','?')}: "
                         f"{a.get('detail', a.get('message', ''))}")

    # Recommendations
    if recommendations:
        lines.append(f"\nTAKTIK ONERILER:")
        for r in recommendations[:5]:
            lines.append(f"  - [{r['type']}] {r.get('message', '')}")

    return "\n".join(lines)


# ── Fallback: rule-based summarizer ─────────────────────────────────────────

def _fallback_brief(context: str) -> str:
    """Generate a basic briefing without LLM."""
    lines = context.split("\n")
    # Extract key stats
    summary = []
    for line in lines:
        line = line.strip()
        if line.startswith("IZLER:") or line.startswith("VARLIKLAR:") or \
           line.startswith("ANOMALILER:") or line.startswith("TAKTIK ONERILER:"):
            summary.append(line)
        elif "YUKSEK" in line and line.startswith("  - "):
            summary.append(f"  DIKKAT: {line.strip()}")

    if not summary:
        return "Mevcut durumda kritik tehdit bulunmuyor. Normal izleme devam ediyor."

    return "\n".join(["[Otomatik Durum Ozeti]", ""] + summary + [
        "", "Not: LLM API yapilandirilmamis. Detayli analiz icin "
        "ANTHROPIC_API_KEY veya OPENAI_API_KEY tanimlayin."
    ])


def _fallback_chat(question: str, context: str) -> str:
    """Answer a question without LLM — simple keyword matching."""
    q = question.lower()

    if any(w in q for w in ["tehdit", "threat", "tehlike", "saldiri", "attack"]):
        # Extract threat lines from context
        threat_lines = [l for l in context.split("\n")
                        if "HIGH" in l or "MEDIUM" in l or "YUKSEK" in l]
        if threat_lines:
            return "Aktif tehditler:\n" + "\n".join(threat_lines[:5])
        return "Su an aktif yuksek seviyeli tehdit bulunmuyor."

    if any(w in q for w in ["durum", "ozet", "status", "brief"]):
        return _fallback_brief(context)

    if any(w in q for w in ["oneri", "ne yapmaliyim", "recommend", "taktik"]):
        rec_lines = [l for l in context.split("\n") if l.strip().startswith("- [")]
        if rec_lines:
            return "Taktik oneriler:\n" + "\n".join(rec_lines[:5])
        return "Su an ozel bir taktik oneri bulunmuyor."

    return ("Sorunuzu anladim ancak LLM API yapilandirilmamis. "
            "Detayli yanit icin ANTHROPIC_API_KEY tanimlayin.\n\n"
            "Soru: " + question)


# ── LLM API calls ──────────────────────────────────────────────────────────

async def _call_anthropic(messages: List[Dict], system: str) -> str:
    """Call Claude API via httpx."""
    import httpx

    model = LLM_MODEL or "claude-sonnet-4-20250514"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1024,
                "system": system,
                "messages": messages,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]


async def _call_openai(messages: List[Dict], system: str) -> str:
    """Call OpenAI API via httpx."""
    import httpx

    model = LLM_MODEL or "gpt-4o"
    all_messages = [{"role": "system", "content": system}] + messages
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1024,
                "messages": all_messages,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def _call_ollama(messages: List[Dict], system: str) -> str:
    """Call local Ollama instance (OpenAI-compatible endpoint)."""
    import httpx

    model = LLM_MODEL or "llama3.2"
    all_messages = [{"role": "system", "content": system}] + messages
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/v1/chat/completions",
            headers={"Content-Type": "application/json"},
            json={
                "model": model,
                "messages": all_messages,
                "stream": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def _call_llm(messages: List[Dict], system: str) -> str:
    """Route to configured LLM provider."""
    if LLM_PROVIDER == "ollama":
        return await _call_ollama(messages, system)
    if LLM_PROVIDER == "openai" and OPENAI_API_KEY:
        return await _call_openai(messages, system)
    elif ANTHROPIC_API_KEY:
        return await _call_anthropic(messages, system)
    elif OPENAI_API_KEY:
        return await _call_openai(messages, system)
    raise ValueError("No LLM API key configured")


# ── Public API ──────────────────────────────────────────────────────────────

# Conversation history per session (simple in-memory)
_conversations: Dict[str, List[Dict]] = {}
MAX_HISTORY = 20


async def get_briefing(
    tracks: Dict[str, Dict],
    threats: Dict[str, Dict],
    assets: Dict[str, Dict],
    zones: Dict[str, Dict],
    anomalies: List[Dict],
    recommendations: List[Dict],
) -> Dict[str, Any]:
    """
    Generate a natural-language situation briefing.
    Returns {"briefing": str, "llm_used": bool}
    """
    context = _build_context(tracks, threats, assets, zones, anomalies, recommendations)

    if not LLM_ENABLED:
        return {"briefing": _fallback_brief(context), "llm_used": False}

    try:
        messages = [{"role": "user",
                     "content": f"Asagidaki COP durum raporunu analiz et ve "
                                f"kisa bir taktik brifing hazirla. Kritik tehditleri "
                                f"ve onerileri vurgula.\n\n{context}"}]
        text = await _call_llm(messages, SYSTEM_PROMPT)
        return {"briefing": text, "llm_used": True}
    except Exception as exc:
        log.warning("[llm] Briefing failed: %s", exc)
        return {"briefing": _fallback_brief(context), "llm_used": False,
                "error": str(exc)}


async def chat(
    question: str,
    tracks: Dict[str, Dict],
    threats: Dict[str, Dict],
    assets: Dict[str, Dict],
    zones: Dict[str, Dict],
    anomalies: List[Dict],
    recommendations: List[Dict],
    session_id: str = "default",
) -> Dict[str, Any]:
    """
    Operator chat: answer a question about the tactical situation.
    Returns {"answer": str, "llm_used": bool}
    """
    context = _build_context(tracks, threats, assets, zones, anomalies, recommendations)

    if not LLM_ENABLED:
        return {"answer": _fallback_chat(question, context), "llm_used": False}

    try:
        # Build conversation with context
        history = _conversations.get(session_id, [])
        messages = history.copy()
        messages.append({
            "role": "user",
            "content": f"[Guncel COP Durumu]\n{context}\n\n"
                       f"[Operator Sorusu]\n{question}",
        })

        text = await _call_llm(messages, SYSTEM_PROMPT)

        # Save to history
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": text})
        if len(history) > MAX_HISTORY * 2:
            history = history[-MAX_HISTORY * 2:]
        _conversations[session_id] = history

        return {"answer": text, "llm_used": True}
    except Exception as exc:
        log.warning("[llm] Chat failed: %s", exc)
        return {"answer": _fallback_chat(question, context), "llm_used": False,
                "error": str(exc)}


async def parse_command(
    command: str,
    tracks: Dict[str, Dict],
    assets: Dict[str, Dict],
) -> Dict[str, Any]:
    """
    Parse a natural-language command into structured actions.
    E.g. "Tum hostile assetleri observe moduna al" -> list of API calls

    Returns {"actions": [...], "explanation": str, "llm_used": bool}
    """
    if not LLM_ENABLED:
        return {
            "actions": [],
            "explanation": "Komut isleme icin LLM API key gerekli. "
                           "ANTHROPIC_API_KEY veya OPENAI_API_KEY tanimlayin.",
            "llm_used": False,
        }

    try:
        context = (
            f"Aktif izler: {json.dumps(list(tracks.keys()))}\n"
            f"Varliklar: {json.dumps({k: v.get('name', k) for k, v in assets.items()})}\n"
        )
        messages = [{
            "role": "user",
            "content": f"""Operator su komutu verdi: "{command}"

Mevcut durum:
{context}

Bu komutu JSON formatinda yapilandirilmis aksiyonlara donustur.
Her aksiyon: {{"action": "approve_task|reject_task|create_asset|delete_asset|create_zone", "params": {{...}}}}

Sadece JSON array dondur, baska bir sey yazma."""
        }]
        text = await _call_llm(messages, SYSTEM_PROMPT)

        # Try to parse JSON from response
        try:
            actions = json.loads(text)
        except json.JSONDecodeError:
            # Try to extract JSON from text
            start = text.find("[")
            end = text.rfind("]")
            if start >= 0 and end > start:
                actions = json.loads(text[start:end + 1])
            else:
                actions = []

        return {"actions": actions, "explanation": text, "llm_used": True}
    except Exception as exc:
        log.warning("[llm] Command parse failed: %s", exc)
        return {"actions": [], "explanation": str(exc), "llm_used": False,
                "error": str(exc)}


def clear_history(session_id: str = "default") -> None:
    _conversations.pop(session_id, None)


def reset() -> None:
    _conversations.clear()
