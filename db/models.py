"""
db/models.py — SQLAlchemy ORM models for NIZAM COP persistence.

Tables:
  users          — operator accounts with roles
  track_events   — hypertable: every track update (TimescaleDB)
  threat_events  — hypertable: every threat update (TimescaleDB)
  alert_records  — hypertable: zone breach alerts (TimescaleDB)
  audit_logs     — hypertable: operator write-action audit trail (TimescaleDB)
  tasks          — operator task records
  zones          — drawn zones (persistent across restarts)
  assets         — placed assets (persistent across restarts)
  waypoints      — mission waypoints (persistent across restarts)
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    Enum as PgEnum,
    Float,
    Integer,
    JSON,
    PrimaryKeyConstraint,
    String,
)
from sqlalchemy.dialects.postgresql import UUID

from db.session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class UserRole(str, enum.Enum):
    ADMIN    = "ADMIN"
    OPERATOR = "OPERATOR"
    VIEWER   = "VIEWER"


class User(Base):
    __tablename__ = "users"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username        = Column(String(64), unique=True, nullable=False, index=True)
    hashed_password = Column(String(256), nullable=False)
    role            = Column(PgEnum(UserRole), default=UserRole.OPERATOR, nullable=False)
    created_at      = Column(DateTime(timezone=True), default=_utcnow)
    last_login      = Column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Time-series (will become TimescaleDB hypertables on init)
# ---------------------------------------------------------------------------

class TrackEvent(Base):
    """One row per cop.track event — hypertable on 'time'."""
    __tablename__ = "track_events"
    __table_args__ = (PrimaryKeyConstraint("id", "time"),)

    id        = Column(UUID(as_uuid=True), default=uuid.uuid4, nullable=False)
    time      = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)
    track_id  = Column(String(64), nullable=False, index=True)
    lat       = Column(Float, nullable=True)
    lon       = Column(Float, nullable=True)
    altitude  = Column(Float, nullable=True)
    speed     = Column(Float, nullable=True)
    heading   = Column(Float, nullable=True)
    source    = Column(String(32), nullable=True)
    raw       = Column(JSON, nullable=True)   # full payload


class ThreatEvent(Base):
    """One row per cop.threat event — hypertable on 'time'."""
    __tablename__ = "threat_events"
    __table_args__ = (PrimaryKeyConstraint("id", "time"),)

    id           = Column(UUID(as_uuid=True), default=uuid.uuid4, nullable=False)
    time         = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)
    track_id     = Column(String(64), nullable=False, index=True)
    threat_level = Column(String(16), nullable=True)
    intent       = Column(String(32), nullable=True)
    score        = Column(Float, nullable=True)
    tti_s        = Column(Float, nullable=True)
    raw          = Column(JSON, nullable=True)


class AlertRecord(Base):
    """Zone-breach alerts — hypertable on 'time'."""
    __tablename__ = "alert_records"
    __table_args__ = (PrimaryKeyConstraint("id", "time"),)

    id        = Column(UUID(as_uuid=True), default=uuid.uuid4, nullable=False)
    time      = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)
    track_id  = Column(String(64), nullable=False)
    zone_id   = Column(String(64), nullable=True)
    zone_name = Column(String(128), nullable=True)
    zone_type = Column(String(32), nullable=True)
    lat       = Column(Float, nullable=True)
    lon       = Column(Float, nullable=True)


class AuditLog(Base):
    """
    Operator write-action audit trail — hypertable on 'time'.

    Tamper evidence: each row carries an entry_hash computed as
        SHA-256(canonical_json || prev_hash)
    where prev_hash is the entry_hash of the immediately preceding row.
    Altering or deleting any past row breaks the chain on every row after it,
    so a compliance verifier can replay the log and detect tampering.
    """
    __tablename__ = "audit_logs"
    __table_args__ = (PrimaryKeyConstraint("id", "time"),)

    id            = Column(UUID(as_uuid=True), default=uuid.uuid4, nullable=False)
    time          = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)
    username      = Column(String(64), nullable=False, index=True)
    role          = Column(String(16), nullable=True)
    action        = Column(String(64), nullable=False, index=True)
    resource_type = Column(String(32), nullable=True)
    resource_id   = Column(String(64), nullable=True)
    detail        = Column(JSON, nullable=True)
    ip            = Column(String(64), nullable=True)
    success       = Column(Integer, default=1)
    prev_hash     = Column(String(64), nullable=True)   # entry_hash of previous row
    entry_hash    = Column(String(64), nullable=True)   # sha256 over canonical repr


# ---------------------------------------------------------------------------
# Operational records (regular tables)
# ---------------------------------------------------------------------------

class TaskRecord(Base):
    __tablename__ = "tasks"

    id           = Column(String(32), primary_key=True)
    track_id     = Column(String(64), nullable=False, index=True)
    action       = Column(String(32), nullable=False)
    threat_level = Column(String(16), nullable=True)
    intent       = Column(String(32), nullable=True)
    score        = Column(Float, nullable=True)
    tti_s        = Column(Float, nullable=True)
    status       = Column(String(16), default="PENDING", nullable=False)
    created_at   = Column(DateTime(timezone=True), default=_utcnow)
    resolved_at  = Column(DateTime(timezone=True), nullable=True)
    resolved_by  = Column(String(64), nullable=True)


class ZoneRecord(Base):
    __tablename__ = "zones"

    id          = Column(String(64), primary_key=True)
    name        = Column(String(128), nullable=False)
    type        = Column(String(32), default="restricted")
    coordinates = Column(JSON, nullable=False)
    color       = Column(String(32), nullable=True)
    created_at  = Column(DateTime(timezone=True), default=_utcnow)


class AssetRecord(Base):
    __tablename__ = "assets"

    id         = Column(String(32), primary_key=True)
    name       = Column(String(128), nullable=False)
    type       = Column(String(32), default="unknown")
    lat        = Column(Float, nullable=False)
    lon        = Column(Float, nullable=False)
    status     = Column(String(32), default="active")
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class WaypointRecord(Base):
    __tablename__ = "waypoints"

    id         = Column(String(32), primary_key=True)
    name       = Column(String(128), nullable=False)
    lat        = Column(Float, nullable=False)
    lon        = Column(Float, nullable=False)
    order      = Column(Integer, default=0)
    mission_id = Column(String(64), default="default")
    created_at = Column(DateTime(timezone=True), default=_utcnow)
