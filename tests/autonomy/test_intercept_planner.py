"""Intercept planner safety tests."""
from __future__ import annotations

import pytest

from services.autonomy.geofence import NoFlyZone
from services.autonomy.intercept_planner import (
    InterceptRefused,
    plan_intercept,
    predict_target_position,
)
from services.autonomy.schemas import InterceptPhase


def _target_track(**overrides) -> dict:
    base = {
        "track_id": "tgt-1",
        "latitude": 39.9334,
        "longitude": 32.8597,
        "altitude": 150.0,
        "vx": 10.0, "vy": 0.0, "vz": 0.0,  # 10 m/s eastward
    }
    base.update(overrides)
    return base


# ── Safety refusals ───────────────────────────────────────────────

def test_no_operator_approval_refuses():
    with pytest.raises(InterceptRefused, match="operator_approved"):
        plan_intercept(_target_track(), operator_approved=False)


def test_track_without_latlon_refuses():
    with pytest.raises(InterceptRefused, match="lat/lon"):
        plan_intercept(
            {"track_id": "t", "x": 0, "y": 0, "vx": 0, "vy": 0},
            operator_approved=True,
        )


def test_waypoint_inside_nfz_refuses():
    # Hedef NFZ merkezinde, hareket yok → waypoint içerde kalacak
    zone = NoFlyZone(
        zone_id="NFZ", name="sivil",
        center_lat=39.9334, center_lon=32.8597, radius_m=2000,
    )
    stationary = _target_track(vx=0.0, vy=0.0)
    with pytest.raises(InterceptRefused, match="NFZ"):
        plan_intercept(stationary, operator_approved=True, no_fly_zones=[zone])


# ── Happy path ────────────────────────────────────────────────────

def test_approved_and_outside_nfz_returns_command():
    cmd = plan_intercept(
        _target_track(), operator_approved=True, approved_by="opr-01",
    )
    assert cmd.phase == InterceptPhase.APPROACH
    assert cmd.operator_approved is True
    assert cmd.approved_by == "opr-01"
    assert cmd.approved_at_iso is not None
    # 5 saniye boyunca doğuya 10 m/s → ~50 m doğuya kaymalı
    assert cmd.waypoint.longitude > 32.8597


def test_predict_target_position_static():
    wp = predict_target_position(39.0, 32.0, 100.0, 0, 0, 0, 5.0)
    assert wp.latitude == pytest.approx(39.0)
    assert wp.longitude == pytest.approx(32.0)
    assert wp.altitude_m == 100.0


def test_predict_target_position_moves_east():
    wp = predict_target_position(0.0, 0.0, 100.0, 10.0, 0.0, 0.0, 10.0)
    # 100 m doğuya — ekvatorda ~0.000898 derece lon
    assert wp.longitude > 0.0
    assert wp.latitude == pytest.approx(0.0)
