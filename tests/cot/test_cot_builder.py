"""CoT XML builder tests."""
from __future__ import annotations

from datetime import datetime, timezone
from xml.etree import ElementTree as ET

from services.cot.cot_builder import (
    COT_TYPE_HOSTILE_UAV,
    build_cot_event,
    serialize,
)


def _fixed_clock() -> datetime:
    return datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)


def test_basic_event_has_required_attrs():
    # Arrange + Act
    ev = build_cot_event(
        uid="NIZAM.t1",
        cot_type=COT_TYPE_HOSTILE_UAV,
        latitude=39.9334,
        longitude=32.8597,
        altitude_hae_m=150.0,
        clock_now=_fixed_clock(),
    )

    # Assert — event root
    assert ev.tag == "event"
    assert ev.attrib["version"] == "2.0"
    assert ev.attrib["uid"] == "NIZAM.t1"
    assert ev.attrib["type"] == COT_TYPE_HOSTILE_UAV
    assert ev.attrib["how"] == "m-g"
    assert ev.attrib["time"].startswith("2026-04-20T12:00:00")


def test_point_has_lat_lon_hae():
    ev = build_cot_event(
        uid="t1", cot_type="a-u-A-M-F-U",
        latitude=41.015, longitude=28.979, altitude_hae_m=500.0,
        clock_now=_fixed_clock(),
    )
    point = ev.find("point")
    assert point is not None
    assert point.attrib["lat"] == "41.0150000"
    assert point.attrib["lon"] == "28.9790000"
    assert point.attrib["hae"] == "500.0"


def test_callsign_renders_contact_tag():
    ev = build_cot_event(
        uid="t1", cot_type="a-u-A", latitude=0.0, longitude=0.0,
        callsign="DJI-M3", clock_now=_fixed_clock(),
    )
    contact = ev.find("detail/contact")
    assert contact is not None
    assert contact.attrib["callsign"] == "DJI-M3"


def test_track_renders_course_speed():
    ev = build_cot_event(
        uid="t1", cot_type="a-u-A", latitude=0.0, longitude=0.0,
        course_deg=45.0, speed_mps=12.5, clock_now=_fixed_clock(),
    )
    track = ev.find("detail/track")
    assert track is not None
    assert track.attrib["course"] == "45.00"
    assert track.attrib["speed"] == "12.50"


def test_remarks_text_and_source():
    ev = build_cot_event(
        uid="t1", cot_type="a-u-A", latitude=0.0, longitude=0.0,
        remarks="hits=5 conf=0.80", clock_now=_fixed_clock(),
    )
    remarks = ev.find("detail/remarks")
    assert remarks is not None
    assert remarks.text == "hits=5 conf=0.80"
    assert remarks.attrib["source"] == "nizam.cop"


def test_stale_is_offset_from_time():
    ev = build_cot_event(
        uid="t1", cot_type="a-u-A", latitude=0.0, longitude=0.0,
        stale_sec=60, clock_now=_fixed_clock(),
    )
    assert ev.attrib["time"].startswith("2026-04-20T12:00:00")
    assert ev.attrib["stale"].startswith("2026-04-20T12:01:00")


def test_serialize_returns_bytes_with_event_root():
    ev = build_cot_event(
        uid="t1", cot_type="a-u-A", latitude=0.0, longitude=0.0,
        clock_now=_fixed_clock(),
    )
    raw = serialize(ev)
    assert raw.startswith(b"<event")
    parsed = ET.fromstring(raw)
    assert parsed.tag == "event"
