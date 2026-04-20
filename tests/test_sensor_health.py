"""Sensor health monitor tests."""
from __future__ import annotations

import pytest

from services.health.sensor_health import HealthMonitor, OFFLINE_TIMEOUT_S


@pytest.mark.asyncio
async def test_new_sensor_added_on_first_message():
    m = HealthMonitor()
    await m.on_message("cam-01", "camera")
    snap = m.snapshot()
    assert len(snap) == 1
    assert snap[0]["sensor_id"] == "cam-01"
    assert snap[0]["online"] is True


@pytest.mark.asyncio
async def test_offline_detected_after_timeout(monkeypatch):
    m = HealthMonitor(offline_timeout_s=0.1)
    await m.on_message("cam-01", "camera")
    import asyncio
    await asyncio.sleep(0.15)
    offline = await m.check_offline()
    assert len(offline) == 1
    assert offline[0].sensor_id == "cam-01"


@pytest.mark.asyncio
async def test_multiple_sensors_tracked():
    m = HealthMonitor()
    await m.on_message("cam-01", "camera")
    await m.on_message("rf-01", "rf_odid")
    await m.on_message("wifi-01", "rf_wifi")
    snap = m.snapshot()
    assert len(snap) == 3
    types = {s["sensor_type"] for s in snap}
    assert types == {"camera", "rf_odid", "rf_wifi"}
