"""Otonom intercept şemaları."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class InterceptPhase(str, Enum):
    IDLE = "idle"
    APPROACH = "approach"       # hedef track'a yaklaş
    SHADOW = "shadow"           # hedefi yakın takip
    ABORT = "abort"             # görev iptal
    RTB = "rtb"                 # return-to-base


class Waypoint(BaseModel):
    """WGS84 waypoint."""
    latitude: float
    longitude: float
    altitude_m: float = 100.0
    speed_mps: float | None = None


class InterceptCommand(BaseModel):
    """Bir intercept drone'una gönderilecek komut."""
    target_track_id: str
    phase: InterceptPhase
    waypoint: Waypoint
    max_approach_distance_m: float = Field(ge=10.0, default=100.0)
    operator_approved: bool  # Her zaman zorunlu
    approved_by: str | None = None
    approved_at_iso: str | None = None


class InterceptState(BaseModel):
    """Intercept drone'un mevcut durumu."""
    drone_id: str
    phase: InterceptPhase
    current_wp: Waypoint | None = None
    target_track_id: str | None = None
    target_distance_m: float | None = None
