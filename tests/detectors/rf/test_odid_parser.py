"""ODID parser tests — ASTM F3411-22 Remote ID."""
from __future__ import annotations

import pytest

from services.detectors.rf.odid_parser import (
    build_basic_id_message,
    build_location_message,
    parse_message,
    parse_message_header,
)
from services.schemas.rf import (
    ODIDBasicID,
    ODIDIDType,
    ODIDLocation,
    ODIDMessageType,
    ODIDUAType,
)


# ── Header parsing ────────────────────────────────────────────────

def test_header_basic_id_protocol_2():
    # Act
    msg_type, proto = parse_message_header(0x02)  # 0x0 << 4 | 0x2

    # Assert
    assert msg_type == ODIDMessageType.BASIC_ID
    assert proto == 2


def test_header_location():
    msg_type, _ = parse_message_header(0x10 | 0x2)
    assert msg_type == ODIDMessageType.LOCATION


# ── Basic ID round-trip ───────────────────────────────────────────

def test_basic_id_roundtrip_drone_serial():
    # Arrange
    message = build_basic_id_message(
        ODIDIDType.SERIAL_NUMBER, ODIDUAType.HELICOPTER_MULTIROTOR, "1581F4ZABCD1234"
    )

    # Act
    msg_type, parsed = parse_message(message)

    # Assert
    assert msg_type == ODIDMessageType.BASIC_ID
    assert isinstance(parsed, ODIDBasicID)
    assert parsed.id_type == ODIDIDType.SERIAL_NUMBER
    assert parsed.ua_type == ODIDUAType.HELICOPTER_MULTIROTOR
    assert parsed.uas_id == "1581F4ZABCD1234"


def test_basic_id_truncates_long_id():
    long_id = "A" * 40
    message = build_basic_id_message(
        ODIDIDType.UTM_ASSIGNED, ODIDUAType.AEROPLANE, long_id
    )
    _, parsed = parse_message(message)
    assert parsed is not None
    assert len(parsed.uas_id) == 20


# ── Location round-trip ───────────────────────────────────────────

def test_location_ankara_coordinates():
    # Arrange — Ankara, Turkey
    message = build_location_message(
        latitude=39.9334, longitude=32.8597, altitude_geo_m=150.0,
        heading_deg=45.0, h_speed_mps=12.5,
    )

    # Act
    msg_type, parsed = parse_message(message)

    # Assert
    assert msg_type == ODIDMessageType.LOCATION
    assert isinstance(parsed, ODIDLocation)
    assert parsed.latitude == pytest.approx(39.9334, rel=1e-6)
    assert parsed.longitude == pytest.approx(32.8597, rel=1e-6)
    assert parsed.altitude_geo_m == pytest.approx(150.0, abs=0.5)
    assert parsed.heading_deg == pytest.approx(45.0, abs=1.0)
    assert parsed.speed_horizontal_mps == pytest.approx(12.5, abs=0.25)


def test_location_heading_over_180_uses_ew_flag():
    message = build_location_message(
        latitude=0.0, longitude=0.0, heading_deg=270.0,
    )
    _, parsed = parse_message(message)
    assert parsed is not None
    assert parsed.heading_deg == pytest.approx(270.0, abs=1.0)


def test_location_zero_coordinates_valid():
    """Lat=0, Lon=0 spec'te "invalid" ama parser literal değeri dönmeli."""
    message = build_location_message(latitude=0.0, longitude=0.0)
    _, parsed = parse_message(message)
    assert parsed is not None
    assert parsed.latitude == 0.0
    assert parsed.longitude == 0.0


# ── Error handling ────────────────────────────────────────────────

def test_parse_message_too_short_raises():
    with pytest.raises(ValueError, match="Mesaj çok kısa"):
        parse_message(b"\x02\x00\x00")  # 3 bayt


def test_unknown_message_type_returns_none_payload():
    """Auth / SelfID / System / OperatorID parser bu servis için öncelik değil."""
    msg = bytes([0x20]) + b"\x00" * 24  # AUTH, protocol 0
    msg_type, parsed = parse_message(msg)
    assert msg_type == ODIDMessageType.AUTH
    assert parsed is None
