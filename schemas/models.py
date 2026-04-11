"""
schemas/models.py  —  Core Pydantic v2 models for NIZAM's event path

Minimal, deliberate scope: only the five objects that cross process or
module boundaries (ingest → STATE → WS → DB). The rest of the codebase
still uses dicts internally — this module exists to validate at the
edges, not to rewrite the world.

Rules:
  - Models are LOAD-FRIENDLY: every field is Optional so they can
    `model_validate()` the kinds of partial payloads that fuser agents
    and adapters currently emit. The goal is validation with a safety
    net, not rejection.
  - `model_config` allows extra fields ("allow") so forward-compatibility
    is preserved — a future field in an adapter doesn't break ingest.
  - `to_dict()` / `from_dict()` round-trip unchanged for existing code
    paths that still pass dicts around.
  - No business rules inside validators. Validation is "is this the
    right shape?", not "does this pass the threat policy?".

Future (not in this module's scope):
  - Strict variants for the /ingest endpoint once the tactical engine
    migrates off dicts
  - JSON Schema export for operator-facing API docs
  - Versioned migration between schema_version="1.1" and future "2.0"
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ── Shared building blocks ──────────────────────────────────────────────────

class Classification(BaseModel):
    """Entity type + confidence + optional identification fields.

    `label` is the canonical taxonomy string: drone / helicopter /
    fixed_wing / missile / vessel / vehicle / bird / balloon / unknown.
    Adapter-specific fields (callsign, squawk, mmsi, vessel_class,
    nav_status) are allowed via extra="allow".
    """
    model_config = ConfigDict(extra="allow")

    label: Optional[str] = None
    conf: Optional[float] = None


class Kinematics(BaseModel):
    """Polar + Cartesian kinematic snapshot. All fields optional — adapters
    fill what they know (ADS-B gives alt+heading; radar gives range+az+vr).
    """
    model_config = ConfigDict(extra="allow")

    range_m:             Optional[float] = None
    az_deg:              Optional[float] = None
    el_deg:              Optional[float] = None
    radial_velocity_mps: Optional[float] = None
    speed_mps:           Optional[float] = None
    heading_deg:         Optional[float] = None
    altitude_m:          Optional[float] = None
    vertical_rate_mps:   Optional[float] = None


# ── 1) Track ────────────────────────────────────────────────────────────────

class Track(BaseModel):
    """Fused track as it lives in STATE["tracks"] and crosses the WS boundary.

    Mirrors the dict shape currently produced by fuser_agent + the ingest
    path, with the fields that actually get read downstream typed.
    """
    model_config = ConfigDict(extra="allow")

    id: Optional[str] = Field(None, description="Canonical track ID (prefer 'id' over 'global_track_id')")
    global_track_id: Optional[str] = None
    track_id: Optional[str] = None

    lat: Optional[float] = None
    lon: Optional[float] = None

    status: Optional[str] = None  # CONFIRMED | TENTATIVE
    classification: Optional[Classification] = None
    kinematics: Optional[Kinematics] = None

    supporting_sensors: List[str] = Field(default_factory=list)
    evidence: List[str] = Field(default_factory=list)

    intent: Optional[str] = None
    intent_conf: Optional[float] = None
    history: List[Dict[str, Any]] = Field(default_factory=list)

    threat_level: Optional[str] = None
    threat_score: Optional[float] = None

    @property
    def canonical_id(self) -> Optional[str]:
        return self.id or self.global_track_id or self.track_id


# ── 2) Threat assessment ────────────────────────────────────────────────────

class Threat(BaseModel):
    """Threat assessment produced by the fusion scorer or ML threat model.

    Emitted on `threat.assessment` events and stored in STATE["threats"].
    """
    model_config = ConfigDict(extra="allow")

    global_track_id: Optional[str] = None
    track_id: Optional[str] = None
    threat_level: Optional[str] = None   # LOW | MEDIUM | HIGH | CRITICAL
    score: Optional[float] = None
    tti_s: Optional[float] = None         # time-to-intercept seconds
    intent: Optional[str] = None
    confidence: Optional[float] = None
    confidence_grade: Optional[str] = None  # A / B / C / D
    ml_probability: Optional[float] = None
    rules_fired: List[str] = Field(default_factory=list)
    reasons: List[str] = Field(default_factory=list)
    recommended_action: Optional[str] = None  # ALERT | OBSERVE | ENGAGE | TAKE_COVER


# ── 3) Lineage record (decision audit trail) ────────────────────────────────

class LineageRecord(BaseModel):
    """One node in a track's decision chain. The chain is stored in
    ai/lineage.py as tamper-evident SHA-256-linked records. This model
    is the on-the-wire shape when it crosses /api/ai/lineage/{track_id}.
    """
    model_config = ConfigDict(extra="allow")

    decision_id: Optional[str] = None
    timestamp: Optional[str] = None
    stage: Optional[str] = None          # ingest | ml_threat | tactical | roe | ...
    summary: Optional[str] = None
    inputs: Dict[str, Any] = Field(default_factory=dict)
    outputs: Dict[str, Any] = Field(default_factory=dict)
    rule: Optional[str] = None
    prev_hash: Optional[str] = None
    hash: Optional[str] = None


# ── 4) BDA record (post-engagement outcome) ─────────────────────────────────

class BdaRecord(BaseModel):
    """Battle Damage Assessment outcome from ai/bda.py.

    An outcome string is the single most important field for the
    operator — it drives the BDA panel colour-coding and the AAR
    hit-rate summary.
    """
    model_config = ConfigDict(extra="allow")

    bda_id: Optional[str] = None
    task_id: Optional[str] = None
    track_id: Optional[str] = None
    action: Optional[str] = None          # ENGAGE | JAM | SPOOF | EW_SUPPRESS
    operator: Optional[str] = None
    engaged_at: Optional[str] = None
    outcome: Optional[str] = None         # DESTROYED | MISS | EVADED | DESTROYED_LATE
    confirmed_at: Optional[str] = None


# ── 5) Task (operator task queue entry) ─────────────────────────────────────

class Task(BaseModel):
    """Task queue entry produced by task_proposer and acted on by the operator.

    Fields follow the existing shape at STATE["tasks"][id]; this model is
    the wire format for /api/tasks and /api/handover.
    """
    model_config = ConfigDict(extra="allow")

    id: Optional[str] = None
    track_id: Optional[str] = None
    target_id: Optional[str] = None
    action: Optional[str] = None         # ENGAGE | OBSERVE | INTERCEPT | MONITOR
    status: Optional[str] = None         # PENDING | APPROVED | REJECTED
    threat_level: Optional[str] = None
    intent: Optional[str] = None
    score: Optional[float] = None
    tti_s: Optional[float] = None
    proposed_by: Optional[str] = None
    created_at: Optional[str] = None
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None


# ── Convenience: event envelope ─────────────────────────────────────────────

class EventEnvelope(BaseModel):
    """Wire envelope for /ingest: every adapter POSTs {event_type, payload}.

    Used as the validation step in the ingest path so adapters that send
    garbage are rejected with a clean 400 instead of breaking inside the
    fusion engine.
    """
    model_config = ConfigDict(extra="allow")

    event_type: str
    payload: Dict[str, Any]


__all__ = [
    "Classification",
    "Kinematics",
    "Track",
    "Threat",
    "LineageRecord",
    "BdaRecord",
    "Task",
    "EventEnvelope",
]
