"""WiFi OUI service tests."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services.detectors.rf.wifi_oui_service import (
    NATSSubject,
    build_wifi_event,
    load_oui_table,
    mac_to_oui,
    match_drone,
    publish_event,
)


def test_subject_format():
    assert NATSSubject.wifi("wifi-01") == "nizam.raw.rf.wifi.wifi-01"


def test_mac_to_oui_colon():
    assert mac_to_oui("60:60:1F:AA:BB:CC") == "60:60:1F"


def test_mac_to_oui_dash():
    assert mac_to_oui("60-60-1F-AA-BB-CC") == "60:60:1F"


def test_mac_to_oui_invalid():
    with pytest.raises(ValueError):
        mac_to_oui("bad")


def test_load_default_table():
    table = load_oui_table()
    assert "60:60:1F" in table
    assert table["60:60:1F"] == "DJI"


def test_match_known_dji_mac():
    table = load_oui_table()
    assert match_drone("60:60:1F:12:34:56", table) == "DJI"


def test_match_unknown_mac():
    table = load_oui_table()
    assert match_drone("AA:BB:CC:11:22:33", table) is None


def test_build_wifi_event_fields():
    event = build_wifi_event(
        "60:60:1F:12:34:56", "DJI", sensor_id="w-01",
        ssid="FreeWifi", rssi_dbm=-65.0, channel=6,
    )
    assert event.vendor == "DJI"
    assert event.oui == "60:60:1F"
    assert event.mac == "60:60:1F:12:34:56"
    assert event.ssid == "FreeWifi"


@pytest.mark.asyncio
async def test_publish_event_uses_correct_subject():
    event = build_wifi_event("60:60:1F:12:34:56", "DJI", sensor_id="w-01")
    nc = AsyncMock()
    await publish_event(nc, event)
    nc.publish.assert_awaited_once()
    assert nc.publish.call_args[0][0] == "nizam.raw.rf.wifi.w-01"
