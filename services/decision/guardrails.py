"""Karar katmanı guardrail'leri — LLM veya rule engine kararından SONRA
çalışır ve güvenlik ihlallerini downgrade eder.

OpenAI Agents guardrail pattern adaptasyonu. Her guardrail:
  - İsim + açıklama döner
  - Decision + context'i inceler
  - Triggered=True dönerse downgrade önerir (LOG/ALERT'e düşür)

Kural: guardrail'ler ASLA upgrade yapmaz, sadece downgrade veya pass.
Bu sayede "false positive" tetiklemesi tehlikeli aksiyon üretmez.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from services.autonomy.geofence import NoFlyZone, haversine_m
from services.decision.schemas import Action, Decision

log = logging.getLogger(__name__)

# Severity sıralama (downgrade için)
_SEVERITY = {Action.LOG: 0, Action.ALERT: 1, Action.HANDOFF: 2, Action.ENGAGE: 3}


@dataclass
class GuardrailResult:
    guardrail_id: str
    triggered: bool
    reason: str = ""
    downgrade_to: Action | None = None


# ── Guardrail 1: Input doğrulama ──────────────────────────────────

def input_track_guardrail(track: dict) -> GuardrailResult:
    """Track bozuksa ALERT/ENGAGE verme — LOG'a düşür.

    - confidence < 0.1 (çok belirsiz)
    - lat/lon 0/0 veya yok (GPS geçersiz)
    - hits < 2 (tek tick, anlık parazit olabilir)
    """
    conf = float(track.get("confidence", 0.0))
    hits = int(track.get("hits", 0))
    lat = float(track.get("latitude", track.get("x", 0.0)) or 0.0)
    lon = float(track.get("longitude", track.get("y", 0.0)) or 0.0)

    if conf < 0.1:
        return GuardrailResult(
            "input-confidence-low", True,
            f"confidence={conf:.2f} < 0.1 — LOG'a düşürülüyor",
            downgrade_to=Action.LOG,
        )
    if hits < 2:
        return GuardrailResult(
            "input-single-tick", True,
            f"hits={hits} — anlık parazit olabilir",
            downgrade_to=Action.LOG,
        )
    if lat == 0.0 and lon == 0.0:
        return GuardrailResult(
            "input-geo-zero", True,
            "lat/lon = 0/0 — GPS geçersiz",
            downgrade_to=Action.LOG,
        )
    return GuardrailResult("input-track", False)


# ── Guardrail 2: Dost bölge kontrolü ──────────────────────────────

@dataclass
class FriendlyZone:
    """Dost üs, kendi drone uçuş alanı, operatör konumu."""
    zone_id: str
    name: str
    center_lat: float
    center_lon: float
    radius_m: float


def _load_friendly_zones_from(path: Path) -> list[FriendlyZone]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw = data.get("zones", []) if isinstance(data, dict) else data
    return [FriendlyZone(**z) for z in raw]


DEFAULT_FRIENDLY_ZONES_PATH = Path("config/friendly_zones.yaml")


def friendly_zone_guardrail(
    track: dict, zones: list[FriendlyZone] | None = None,
) -> GuardrailResult:
    """Track dost bölge içinde ise ENGAGE/HANDOFF → ALERT'e düşür.

    "Kendi drone'unu vurma" kuralı. Gerçek savunma sisteminde dost bölgeler
    friendly_zones.yaml'a kayıtlı olmalı.
    """
    if zones is None:
        zones = _load_friendly_zones_from(DEFAULT_FRIENDLY_ZONES_PATH)
    if not zones:
        return GuardrailResult("friendly-zone", False, "no friendly zones configured")

    lat = track.get("latitude")
    lon = track.get("longitude")
    if lat is None or lon is None:
        return GuardrailResult("friendly-zone", False, "no lat/lon")

    for zone in zones:
        dist = haversine_m(float(lat), float(lon), zone.center_lat, zone.center_lon)
        if dist <= zone.radius_m:
            return GuardrailResult(
                f"friendly-zone-{zone.zone_id}", True,
                f"track {zone.name} dost bölgesinde (dist={dist:.0f}m) — ENGAGE yasak",
                downgrade_to=Action.ALERT,
            )
    return GuardrailResult("friendly-zone", False)


# ── Guardrail 3: Sivil trafik deseni ──────────────────────────────

def civilian_pattern_guardrail(track: dict) -> GuardrailResult:
    """Sivil hava trafiği deseni tespit ederse ENGAGE'i iptal et.

    Heuristikler:
    - Bilinen ADS-B transponder kodu varsa (ICAO / squawk)
    - Hız > 150 m/s (helikopterden hızlı — muhtemelen uçak)
    - Altitude > 3000m (tipik sivil irtifa)
    """
    uas_id = str(track.get("uas_id") or "")
    vx = float(track.get("vx", 0.0))
    vy = float(track.get("vy", 0.0))
    speed = (vx * vx + vy * vy) ** 0.5
    alt = float(track.get("altitude", track.get("z", 0.0)) or 0.0)

    # Bilinen sivil transponder prefix'leri
    civil_prefixes = ("TC-", "N-", "D-", "G-", "F-")  # TR, US, DE, UK, FR
    if uas_id and any(uas_id.upper().startswith(p) for p in civil_prefixes):
        return GuardrailResult(
            "civilian-transponder", True,
            f"uas_id={uas_id} sivil tescil",
            downgrade_to=Action.ALERT,
        )

    if speed > 150.0 and alt > 3000.0:
        return GuardrailResult(
            "civilian-airliner-pattern", True,
            f"speed={speed:.0f}m/s alt={alt:.0f}m — muhtemelen sivil uçak",
            downgrade_to=Action.ALERT,
        )

    return GuardrailResult("civilian-pattern", False)


# ── Orkestratör ───────────────────────────────────────────────────

ALL_GUARDRAILS = [
    input_track_guardrail,
    friendly_zone_guardrail,
    civilian_pattern_guardrail,
]


def apply_guardrails(
    decision: Decision, track: dict,
    friendly_zones: list[FriendlyZone] | None = None,
) -> Decision:
    """Tüm guardrail'leri çalıştır, tetiklenenleri decision'a ekle ve downgrade uygula.

    Downgrade kuralı: mevcut action'dan **daha düşük** severity'e inen
    guardrail'in downgrade_to'su yeni action olur. Guardrail upgrade YAPMAZ.
    """
    final_action = decision.action
    triggered_ids: list[str] = []
    reasons: list[str] = []

    for guard in ALL_GUARDRAILS:
        if guard is friendly_zone_guardrail:
            result = guard(track, zones=friendly_zones)
        else:
            result = guard(track)
        if not result.triggered:
            continue
        triggered_ids.append(result.guardrail_id)
        reasons.append(result.reason)
        if result.downgrade_to is None:
            continue
        proposed = result.downgrade_to
        if _SEVERITY[proposed] < _SEVERITY[final_action]:
            final_action = proposed

    if not triggered_ids:
        return decision

    new_reasoning = f"{decision.reasoning} | guardrails: {'; '.join(reasons)}"
    log.info("guardrails triggered: %s → action %s → %s",
             triggered_ids, decision.action.value, final_action.value)

    return decision.model_copy(update={
        "action": final_action,
        "reasoning": new_reasoning[:500],
        "guardrails_triggered": triggered_ids,
        "requires_operator_approval":
            decision.requires_operator_approval or final_action == Action.ENGAGE,
    })
