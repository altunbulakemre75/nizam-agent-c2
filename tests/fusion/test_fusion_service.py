"""FusionService orchestrator tests (pure functions + queue logic)."""
from __future__ import annotations

import pytest

from services.fusion.fusion_service import (
    camera_to_measurements,
    enu_to_latlon,
    latlon_to_enu,
    odid_to_measurement,
)


ANKARA = (39.9334, 32.8597)


def test_latlon_enu_roundtrip():
    e, n = latlon_to_enu(39.95, 32.87, *ANKARA)
    lat2, lon2 = enu_to_latlon(e, n, *ANKARA)
    assert abs(lat2 - 39.95) < 1e-6
    assert abs(lon2 - 32.87) < 1e-6


def test_origin_enu_is_zero():
    e, n = latlon_to_enu(*ANKARA, *ANKARA)
    assert abs(e) < 1e-6
    assert abs(n) < 1e-6


def test_odid_without_location_returns_none():
    msg = {
        "sensor_id": "rf-01", "timestamp_iso": "2026-04-20T00:00:00Z",
        "source": "bluetooth-le", "location": None,
    }
    assert odid_to_measurement(msg, *ANKARA) is None


def test_odid_with_location_maps_to_measurement():
    msg = {
        "sensor_id": "rf-01", "timestamp_iso": "2026-04-20T00:00:00Z",
        "source": "bluetooth-le",
        "basic_id": {"uas_id": "DJI-123", "ua_type": "HELICOPTER_MULTIROTOR"},
        "location": {
            "latitude": 39.95, "longitude": 32.87, "altitude_geo_m": 120.0,
        },
    }
    m = odid_to_measurement(msg, *ANKARA)
    assert m is not None
    assert m.sensor_type.value == "rf_odid"
    assert m.uas_id == "DJI-123"
    assert m.z == pytest.approx(120.0)


def test_camera_empty_detections():
    msg = {
        "sensor_id": "cam-01", "timestamp_iso": "2026-04-20T00:00:00Z",
        "detections": [],
    }
    ms = camera_to_measurements(msg, *ANKARA, sensor_lat=ANKARA[0], sensor_lon=ANKARA[1])
    assert ms == []


def test_camera_with_detection_emits_measurement():
    msg = {
        "sensor_id": "cam-01", "timestamp_iso": "2026-04-20T00:00:00Z",
        "detections": [
            {"bbox": {"x1": 0, "y1": 0, "x2": 10, "y2": 10},
             "conf": 0.9, "class_id": 0, "class_name": "drone"},
        ],
    }
    ms = camera_to_measurements(msg, *ANKARA, sensor_lat=ANKARA[0], sensor_lon=ANKARA[1])
    assert len(ms) == 1
    assert ms[0].class_name == "drone"
    assert ms[0].class_conf == pytest.approx(0.9)
