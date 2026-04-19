"""ODID event → CoT tests."""
from __future__ import annotations

from datetime import datetime, timezone

from services.cot.cot_builder import COT_TYPE_UNKNOWN_UAV
from services.cot.odid_to_cot import odid_event_to_cot


def _fixed() -> datetime:
    return datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)


def test_odid_without_location_returns_none():
    event = {
        "sensor_id": "rf-01",
        "source": "bluetooth-le",
        "basic_id": {"uas_id": "X", "ua_type": "HELICOPTER_MULTIROTOR"},
        "location": None,
    }
    assert odid_event_to_cot(event, clock_now=_fixed()) is None


def test_odid_with_location_emits_unknown_uav():
    event = {
        "sensor_id": "rf-01",
        "source": "bluetooth-le",
        "rssi_dbm": -68.0,
        "basic_id": {"uas_id": "DJI-M3-ABC", "ua_type": "HELICOPTER_MULTIROTOR"},
        "location": {
            "latitude": 39.9334,
            "longitude": 32.8597,
            "altitude_geo_m": 120.0,
            "heading_deg": 270.0,
            "speed_horizontal_mps": 15.5,
        },
    }
    ev = odid_event_to_cot(event, clock_now=_fixed())
    assert ev is not None
    assert ev.attrib["type"] == COT_TYPE_UNKNOWN_UAV
    assert ev.attrib["uid"] == "NIZAM.ODID.DJI-M3-ABC"
    point = ev.find("point")
    assert point is not None
    assert point.attrib["lat"] == "39.9334000"
    assert point.attrib["hae"] == "120.0"
    track = ev.find("detail/track")
    assert track is not None
    assert track.attrib["course"] == "270.00"
    assert track.attrib["speed"] == "15.50"
    remarks = ev.find("detail/remarks")
    assert remarks is not None
    assert "sensor=rf-01" in (remarks.text or "")
    assert "rssi=-68dBm" in (remarks.text or "")
