"""Kamera kalibrasyon tests."""
from __future__ import annotations

import pytest

from services.detectors.camera.calibration import (
    CameraCalibration,
    bbox_center_to_bearing,
    load_calibration,
    project_bbox_to_position,
)


def _cam(**kwargs) -> CameraCalibration:
    base = dict(
        sensor_id="t", latitude=39.9, longitude=32.8, altitude_m=900.0,
        heading_deg=0.0, fov_h_deg=60.0, fov_v_deg=40.0, nominal_range_m=200.0,
    )
    base.update(kwargs)
    return CameraCalibration(**base)


def test_default_when_no_config():
    cal = load_calibration("nonexistent-cam")
    assert cal.latitude != 0.0
    assert cal.sensor_id == "nonexistent-cam"


def test_bbox_center_bearing_middle_equals_heading():
    cal = _cam(heading_deg=90.0)  # east
    bearing, elevation = bbox_center_to_bearing(0.5, 0.5, cal)
    assert bearing == pytest.approx(90.0)
    assert elevation == pytest.approx(0.0)


def test_bbox_right_shifts_bearing():
    cal = _cam(heading_deg=0.0, fov_h_deg=60.0)
    # Frame'in sağ kenarında (cx=1.0)
    bearing, _ = bbox_center_to_bearing(1.0, 0.5, cal)
    assert bearing == pytest.approx(30.0)  # heading + fov_h/2


def test_bbox_top_positive_elevation():
    cal = _cam(fov_v_deg=40.0)
    _, elevation = bbox_center_to_bearing(0.5, 0.0, cal)
    assert elevation == pytest.approx(20.0)


def test_project_center_yields_reference_plus_nominal():
    cal = _cam(heading_deg=0.0, nominal_range_m=1000.0)
    # Frame merkezi: 640x480'de bbox 310,230 - 330,250
    lat, lon, _ = project_bbox_to_position(310, 230, 330, 250, 640, 480, cal)
    assert lat > cal.latitude
    assert abs(lon - cal.longitude) < 0.0001


def test_project_with_explicit_range_override():
    cal = _cam(nominal_range_m=200.0)
    lat1, _, _ = project_bbox_to_position(310, 230, 330, 250, 640, 480, cal, range_m=2000)
    lat2, _, _ = project_bbox_to_position(310, 230, 330, 250, 640, 480, cal, range_m=200)
    # 10x range → 10x lat shift (yaklaşık)
    shift1 = abs(lat1 - cal.latitude)
    shift2 = abs(lat2 - cal.latitude)
    assert shift1 > 5 * shift2
