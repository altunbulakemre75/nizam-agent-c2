"""5-node LangGraph state machine — LLM danışman akışı.

LangGraph kurulu değilse aynı akışı saf Python ile koşturur (fallback).
Her iki yol da aynı DecisionTrace döndürür → audit trail.

Nodes:
  1. classify     — rule engine + LLM ile tehdit sınıflandır
  2. retrieve_roe — ROE RAG ile doktrin bağlamı çek
  3. reason       — LLM ile aksiyon önerisi (context + ROE)
  4. guardrail    — guardrails.apply_guardrails() downgrade
  5. finalize     — Decision oluştur + (varsa) PostgreSQL checkpoint
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from services.decision.guardrails import FriendlyZone, apply_guardrails
from services.decision.llm_client import LLMResponse, query_llm
from services.decision.roe import evaluate_roe
from services.decision.rules import assess_threat
from services.decision.schemas import (
    Action,
    Decision,
    DecisionSource,
    ROERule,
    ThreatAssessment,
    ThreatLevel,
)

log = logging.getLogger(__name__)


@dataclass
class GraphState:
    """5-node arası taşınan state."""
    track: dict
    roe_rules: list[ROERule]
    friendly_zones: list[FriendlyZone]
    inside_protected_zone: bool = False
    heading_toward_zone: bool = False

    # Node çıktıları
    assessment: ThreatAssessment | None = None
    rule_action: Action | None = None
    rule_ref: str | None = None
    rule_approval_required: bool = False
    roe_context: list[dict] = field(default_factory=list)     # {rule_id, excerpt}
    llm_response: LLMResponse | None = None
    decision: Decision | None = None


# ── Node 1: classify ──────────────────────────────────────────────

async def classify(state: GraphState) -> GraphState:
    state.assessment = assess_threat(
        state.track,
        inside_protected_zone=state.inside_protected_zone,
        heading_toward_zone=state.heading_toward_zone,
    )
    action, matched = evaluate_roe(
        state.roe_rules, state.assessment.threat_level, state.inside_protected_zone,
    )
    state.rule_action = action
    state.rule_ref = matched.rule_id if matched else None
    state.rule_approval_required = bool(matched and matched.requires_operator_approval) or action == Action.ENGAGE
    return state


# ── Node 2: retrieve_roe (RAG) ────────────────────────────────────

async def retrieve_roe(state: GraphState) -> GraphState:
    try:
        from services.knowledge.roe_rag import ROERAG

        rag = ROERAG()
        query = (
            f"threat level {state.assessment.threat_level.value} "
            f"{'inside protected zone' if state.inside_protected_zone else 'outside zone'}"
        )
        results = rag.query(query, top_k=3)
        state.roe_context = [
            {"rule_id": r.rule_id, "excerpt": r.excerpt, "source": r.source}
            for r in results
        ]
    except Exception as exc:
        log.debug("ROE RAG atlandı: %s", exc)
    return state


# ── Node 3: reason (LLM advisor) ──────────────────────────────────

def _is_llm_enabled() -> bool:
    return os.getenv("NIZAM_DECISION_LLM_ENABLED", "false").lower() == "true"


async def reason(state: GraphState) -> GraphState:
    if not _is_llm_enabled():
        return state

    from services.decision.sanitize import UnsafeContent, safe_free_text, sanitize_track_for_llm

    assessment = state.assessment
    assert assessment is not None

    # PROMPT INJECTION DEFENSE — attacker uas_id/class_name alanlarına payload koyamaz
    try:
        t = sanitize_track_for_llm(state.track)
    except UnsafeContent as exc:
        log.warning("Track sanitize başarısız (injection): %s — LLM atlanıyor", exc)
        return state

    roe_lines = []
    for r in state.roe_context[:2]:
        try:
            excerpt = safe_free_text(r.get("excerpt", ""), max_len=200, reject_injection=False)
            rule_id = (r.get("rule_id") or r.get("source") or "")[:40]
            if excerpt:
                roe_lines.append(f"- [{rule_id}] {excerpt}")
        except Exception:
            continue
    roe_text = "\n".join(roe_lines) or "(no doctrine retrieved)"

    rule_reasoning = safe_free_text(assessment.reasoning, max_len=300, reject_injection=False)

    prompt = (
        f"Counter-UAS track advisor. You are NOT the decision-maker — rule engine is.\n"
        f"Track id={t['track_id']} conf={t['confidence']:.2f}\n"
        f"Pos ENU x={t['x']:.0f} y={t['y']:.0f} z={t['z']:.0f}\n"
        f"Velocity vx={t['vx']:.1f} vy={t['vy']:.1f}\n"
        f"UAS ID: {t['uas_id']}  Class: {t['class_name']}  Sources: {t['sources']}\n\n"
        f"Rule engine pre-assessment:\n"
        f"  threat_level={assessment.threat_level.value} score={assessment.score:.2f}\n"
        f"  reasoning={rule_reasoning}\n"
        f"  proposed_action={state.rule_action.value if state.rule_action else 'log'}\n\n"
        f"Relevant doctrine:\n{roe_text}\n\n"
        f"Submit your independent advisor recommendation. Valid actions: "
        f"log, alert, handoff. ENGAGE is reserved for operators only."
    )

    state.llm_response = await query_llm(prompt)
    return state


# ── Node 4: guardrail ─────────────────────────────────────────────

def _reconcile_action(rule_action: Action, llm_response: LLMResponse | None) -> Action:
    """LLM sadece rule'dan YÜKSEK severity'e upgrade edebilir; asla ENGAGE olamaz.

    Downgrade (LOG'a düşürme) guardrail'lerin işi.
    """
    severity = {Action.LOG: 0, Action.ALERT: 1, Action.HANDOFF: 2, Action.ENGAGE: 3}
    if llm_response is None:
        return rule_action
    try:
        llm_action = Action(llm_response.action)
    except ValueError:
        return rule_action
    if llm_action == Action.ENGAGE:          # safety: Claude bile olmaz
        return rule_action
    if severity[llm_action] > severity[rule_action]:
        return llm_action
    return rule_action


async def guardrail(state: GraphState) -> GraphState:
    assert state.assessment is not None
    assert state.rule_action is not None

    merged_action = _reconcile_action(state.rule_action, state.llm_response)
    base_reasoning = state.assessment.reasoning
    if state.llm_response is not None:
        base_reasoning += f" | LLM({state.llm_response.provider}): {state.llm_response.reasoning[:200]}"

    pre_decision = Decision(
        track_id=state.track["track_id"],
        action=merged_action,
        threat_level=state.assessment.threat_level,
        confidence=float(state.track.get("confidence", 0.0)),
        reasoning=base_reasoning[:500],
        source=DecisionSource.LLM_ADVISOR if state.llm_response else DecisionSource.RULE_ENGINE,
        roe_reference=state.rule_ref,
        requires_operator_approval=state.rule_approval_required or merged_action == Action.ENGAGE,
        timestamp_iso=datetime.now(timezone.utc).isoformat(),
        llm_raw_response=state.llm_response.raw if state.llm_response else None,
        llm_provider=state.llm_response.provider if state.llm_response else None,
        llm_model=state.llm_response.model if state.llm_response else None,
    )

    state.decision = apply_guardrails(pre_decision, state.track, friendly_zones=state.friendly_zones)
    return state


# ── Node 5: finalize (checkpoint) ─────────────────────────────────

async def finalize(state: GraphState) -> GraphState:
    """PostgreSQL checkpoint — kararı decisions tablosuna yaz.

    DB bağlantısı yoksa sessizce atlanır; decision yine de dönülür.
    """
    if state.decision is None:
        return state

    dsn = os.getenv("NIZAM_DB_DSN")
    if not dsn:
        return state

    try:
        import asyncpg

        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS decisions (
                    id SERIAL PRIMARY KEY,
                    track_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    threat_level TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    reasoning TEXT,
                    source TEXT,
                    roe_reference TEXT,
                    requires_operator_approval BOOLEAN,
                    timestamp_iso TIMESTAMPTZ NOT NULL,
                    llm_provider TEXT,
                    llm_model TEXT,
                    llm_raw_response JSONB,
                    guardrails_triggered TEXT[]
                )
                """
            )
            d = state.decision
            await conn.execute(
                """INSERT INTO decisions(track_id,action,threat_level,confidence,reasoning,
                   source,roe_reference,requires_operator_approval,timestamp_iso,
                   llm_provider,llm_model,llm_raw_response,guardrails_triggered)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::timestamptz,$10,$11,$12::jsonb,$13)""",
                d.track_id, d.action.value, d.threat_level.value, d.confidence, d.reasoning,
                d.source.value, d.roe_reference, d.requires_operator_approval, d.timestamp_iso,
                d.llm_provider, d.llm_model,
                __import__("json").dumps(d.llm_raw_response) if d.llm_raw_response else None,
                d.guardrails_triggered,
            )
        finally:
            await conn.close()
    except Exception as exc:
        log.warning("decision checkpoint başarısız: %s", exc)

    return state


# ── Orkestratör ───────────────────────────────────────────────────

async def run_graph(
    track: dict,
    roe_rules: list[ROERule],
    friendly_zones: list[FriendlyZone] | None = None,
    inside_protected_zone: bool = False,
    heading_toward_zone: bool = False,
) -> Decision:
    """5-node akışı sırayla çalıştır. LangGraph kurulu ise StateGraph kullanır."""
    state = GraphState(
        track=track, roe_rules=roe_rules,
        friendly_zones=friendly_zones or [],
        inside_protected_zone=inside_protected_zone,
        heading_toward_zone=heading_toward_zone,
    )

    try:
        from langgraph.graph import StateGraph, END   # type: ignore

        workflow = StateGraph(GraphState)
        workflow.add_node("classify", classify)
        workflow.add_node("retrieve_roe", retrieve_roe)
        workflow.add_node("reason", reason)
        workflow.add_node("guardrail", guardrail)
        workflow.add_node("finalize", finalize)
        workflow.set_entry_point("classify")
        workflow.add_edge("classify", "retrieve_roe")
        workflow.add_edge("retrieve_roe", "reason")
        workflow.add_edge("reason", "guardrail")
        workflow.add_edge("guardrail", "finalize")
        workflow.add_edge("finalize", END)
        graph = workflow.compile()
        final_state = await graph.ainvoke(state)
        return final_state.decision   # type: ignore

    except ImportError:
        # Fallback — saf sequential
        state = await classify(state)
        state = await retrieve_roe(state)
        state = await reason(state)
        state = await guardrail(state)
        state = await finalize(state)
        assert state.decision is not None
        return state.decision
