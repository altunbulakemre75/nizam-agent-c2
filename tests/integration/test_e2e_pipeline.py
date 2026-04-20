"""E2E pipeline testi — sensör ölçümü → fusion → track → CoT → decision.

NATS'siz çalışır — saf Python ile tüm pipeline adımlarını tek testte
doğrular.
"""
from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from services.cot.cot_builder import COT_TYPE_HOSTILE_UAV
from services.cot.enrichment import enrich_event
from services.cot.fusion_to_cot import track_to_cot
from services.cot.validator import validate_structure
from services.decision.roe import load_roe
from services.decision.schemas import Action, ThreatLevel
from services.decision.threat_graph import decide
from services.fusion.track_manager import TrackManager
from services.schemas.track import Measurement, SensorType


CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "roe" / "default.yaml"


def _meas(x: float, y: float, sensor: SensorType = SensorType.CAMERA) -> Measurement:
    return Measurement(
        sensor_id="e2e-cam", sensor_type=sensor,
        timestamp_iso="2026-04-20T00:00:00+00:00",
        x=x, y=y, z=100.0, sigma_x=3.0, sigma_y=3.0, sigma_z=5.0,
    )


def test_full_pipeline_low_threat_logs():
    # 1. Sensör ölçümleri → Fusion (3 hit)
    tm = TrackManager(n_confirm=3)
    for _ in range(3):
        tracks = tm.step([_meas(1000.0, 1000.0)], dt=0.1)
    assert len(tracks) == 1
    track = tracks[0]
    assert track.state.value == "confirmed"

    # 2. Fusion track → Decision (LOW threat)
    rules = load_roe(CONFIG_PATH)
    track_dict = track.model_dump()
    # LOW skorunu zorla: sürati düşür, zone dışı
    track_dict["vx"] = 0.0
    track_dict["vy"] = 0.0
    track_dict["confidence"] = 0.2
    assessment, decision = decide(track_dict, rules, inside_protected_zone=False)
    assert assessment.threat_level == ThreatLevel.LOW
    assert decision.action == Action.LOG

    # 3. Track → CoT XML
    cot = track_to_cot(track_dict, ref_lat=39.9, ref_lon=32.8)
    enrich_event(cot)
    validate_structure(cot)

    # 4. CoT doğru tipte
    assert cot.attrib["type"].startswith("a-u-A")  # unknown UAV


def test_full_pipeline_critical_engages_only_with_approval():
    tm = TrackManager(n_confirm=1)
    tm.step([_meas(100.0, 100.0)], dt=0.1)
    tracks = tm.step([_meas(100.5, 100.5)], dt=0.1)
    track = tracks[0]

    track_dict = track.model_dump()
    track_dict["vx"] = 100.0
    track_dict["vy"] = 100.0
    track_dict["confidence"] = 1.0

    rules = load_roe(CONFIG_PATH)
    _, decision = decide(
        track_dict, rules,
        inside_protected_zone=True, heading_toward_zone=True,
    )
    # CRITICAL olmasına rağmen default ROE'da ENGAGE disabled → LOG'a düşer
    assert decision.action != Action.ENGAGE, "SAFETY: default ENGAGE disabled"


def test_cot_validates_for_arbitrary_track():
    track_dict = {
        "track_id": "t-abc", "state": "confirmed",
        "x": 100.0, "y": 200.0, "z": 150.0,
        "vx": 5.0, "vy": 5.0, "vz": 0.0,
        "confidence": 0.9, "hits": 10, "sources": ["camera", "rf_odid"],
        "uas_id": "DJI-M3-X", "class_name": "quadcopter",
    }
    cot = track_to_cot(track_dict, ref_lat=39.9, ref_lon=32.8)
    validate_structure(cot)
    assert cot.attrib["type"] == COT_TYPE_HOSTILE_UAV
