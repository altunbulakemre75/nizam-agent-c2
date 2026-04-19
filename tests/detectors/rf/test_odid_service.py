"""ODID service (event builder + NATS publisher) tests."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services.detectors.rf.odid_parser import build_basic_id_message, build_location_message
from services.detectors.rf.odid_service import NATSSubject, build_odid_event, publish_event
from services.schemas.rf import ODIDEvent, ODIDIDType, ODIDUAType


def test_subject_format():
    assert NATSSubject.odid("rf-01") == "nizam.raw.rf.odid.rf-01"
    assert NATSSubject.odid("edge-west") == "nizam.raw.rf.odid.edge-west"


def test_build_event_from_basic_id():
    # Arrange
    raw = build_basic_id_message(
        ODIDIDType.SERIAL_NUMBER, ODIDUAType.HELICOPTER_MULTIROTOR, "DJIM3-12345"
    )

    # Act
    event = build_odid_event(raw, sensor_id="rf-01", source="bluetooth-le", rssi_dbm=-72.5)

    # Assert
    assert isinstance(event, ODIDEvent)
    assert event.sensor_id == "rf-01"
    assert event.source == "bluetooth-le"
    assert event.rssi_dbm == -72.5
    assert event.basic_id is not None
    assert event.basic_id.uas_id == "DJIM3-12345"
    assert event.location is None


def test_build_event_from_location():
    raw = build_location_message(latitude=41.015, longitude=28.979, altitude_geo_m=80.0)
    event = build_odid_event(raw, sensor_id="rf-02", source="wifi-nan")
    assert event is not None
    assert event.location is not None
    assert event.location.latitude == pytest.approx(41.015, rel=1e-6)
    assert event.basic_id is None


def test_build_event_invalid_bytes_returns_none():
    event = build_odid_event(b"\x00\x00", sensor_id="rf-01", source="mock")
    assert event is None


def test_build_event_unknown_message_type_returns_none():
    # Auth message — parsed but payload is None, service skips it
    auth_msg = bytes([0x22]) + b"\x00" * 24  # AUTH (0x2), protocol 2
    event = build_odid_event(auth_msg, sensor_id="rf-01", source="bluetooth-le")
    assert event is None


@pytest.mark.asyncio
async def test_publish_event_sends_to_correct_subject():
    # Arrange
    raw = build_basic_id_message(ODIDIDType.SERIAL_NUMBER, ODIDUAType.AEROPLANE, "X1")
    event = build_odid_event(raw, sensor_id="rf-test", source="bluetooth-le")
    assert event is not None

    nc = AsyncMock()

    # Act
    await publish_event(nc, event)

    # Assert
    nc.publish.assert_awaited_once()
    subject = nc.publish.call_args[0][0]
    assert subject == "nizam.raw.rf.odid.rf-test"
