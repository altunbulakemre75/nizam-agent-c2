"""Track → CoT tests."""
from __future__ import annotations

from datetime import datetime, timezone

from services.cot.cot_builder import COT_TYPE_HOSTILE_UAV, COT_TYPE_UNKNOWN_UAV
from services.cot.fusion_to_cot import (
    enu_to_latlon,
    track_cot_type,
    track_to_cot,
)


ANKARA = (39.9334, 32.8597)


def test_enu_origin_is_ref_point():
    lat, lon = enu_to_latlon(0.0, 0.0, *ANKARA)
    assert lat == ANKARA[0]
    assert lon == ANKARA[1]


def test_enu_1km_north_shifts_latitude():
    _, lon = enu_to_latlon(0.0, 1000.0, *ANKARA)
    lat, _ = enu_to_latlon(0.0, 1000.0, *ANKARA)
    assert lat > ANKARA[0]
    assert lon == ANKARA[1]
    # 1 km kuzeye ~0.009 derece
    assert abs((lat - ANKARA[0]) - 0.009) < 0.001


def test_cot_type_confirmed_high_conf_is_hostile():
    assert track_cot_type("confirmed", 0.9) == COT_TYPE_HOSTILE_UAV


def test_cot_type_confirmed_low_conf_is_unknown():
    assert track_cot_type("confirmed", 0.3) == COT_TYPE_UNKNOWN_UAV


def test_cot_type_tentative_always_unknown():
    assert track_cot_type("tentative", 0.99) == COT_TYPE_UNKNOWN_UAV


def test_track_to_cot_hostile_roundtrip():
    track = {
        "track_id": "t-abc123def",
        "state": "confirmed",
        "x": 1000.0, "y": 500.0, "z": 150.0,
        "vx": 10.0, "vy": 0.0, "vz": 0.0,
        "confidence": 0.9,
        "hits": 10,
        "sources": ["camera", "rf_odid"],
        "uas_id": "DJI-M3-X1",
        "class_name": "quadcopter",
    }
    ev = track_to_cot(
        track, ref_lat=ANKARA[0], ref_lon=ANKARA[1],
        clock_now=datetime(2026, 4, 20, tzinfo=timezone.utc),
    )
    assert ev.attrib["type"] == COT_TYPE_HOSTILE_UAV
    assert ev.attrib["uid"] == "NIZAM.t-abc123def"
    contact = ev.find("detail/contact")
    assert contact is not None
    assert contact.attrib["callsign"] == "DJI-M3-X1"
    remarks = ev.find("detail/remarks")
    assert remarks is not None
    assert "hits=10" in (remarks.text or "")
    assert "conf=0.90" in (remarks.text or "")


def test_track_with_latlon_skips_enu_conversion():
    track = {
        "track_id": "t1",
        "state": "confirmed",
        "x": 0.0, "y": 0.0, "z": 100.0,
        "vx": 0.0, "vy": 0.0, "vz": 0.0,
        "confidence": 0.8,
        "latitude": 41.015,
        "longitude": 28.979,
        "altitude": 250.0,
    }
    ev = track_to_cot(track, ref_lat=0.0, ref_lon=0.0)
    point = ev.find("point")
    assert point is not None
    assert point.attrib["lat"] == "41.0150000"
    assert point.attrib["lon"] == "28.9790000"
    assert point.attrib["hae"] == "250.0"
