"""TrackManager lifecycle tests — tentative → confirmed → lost → deleted."""
from __future__ import annotations

from services.fusion.track_manager import TrackManager
from services.schemas.track import Measurement, SensorType, TrackState


def _meas(x: float, y: float, sensor: SensorType = SensorType.CAMERA) -> Measurement:
    return Measurement(
        sensor_id="cam-01",
        sensor_type=sensor,
        timestamp_iso="2026-04-20T00:00:00+00:00",
        x=x, y=y, z=0.0,
        sigma_x=3.0, sigma_y=3.0, sigma_z=5.0,
    )


def test_spawn_tentative_on_first_measurement():
    tm = TrackManager()
    tracks = tm.step([_meas(100.0, 100.0)], dt=0.1)
    assert len(tracks) == 1
    assert tracks[0].state == TrackState.TENTATIVE
    assert tracks[0].hits == 1


def test_tentative_becomes_confirmed_after_n_hits():
    tm = TrackManager(n_confirm=3)
    for _ in range(3):
        tracks = tm.step([_meas(50.0, 50.0)], dt=0.1)
    assert tracks[0].state == TrackState.CONFIRMED
    assert tracks[0].hits >= 3


def test_confirmed_becomes_lost_after_m_misses():
    tm = TrackManager(n_confirm=2, m_lost=3)
    # Önce confirm
    tm.step([_meas(0.0, 0.0)], dt=0.1)
    tm.step([_meas(0.0, 0.0)], dt=0.1)
    # 3 boş tick
    for _ in range(3):
        tracks = tm.step([], dt=0.1)
    assert tracks[0].state == TrackState.LOST


def test_tentative_deleted_fast_on_misses():
    tm = TrackManager(n_confirm=5, m_lost=2)
    tm.step([_meas(0.0, 0.0)], dt=0.1)
    # 2 miss → tentative deleted
    tm.step([], dt=0.1)
    tracks = tm.step([], dt=0.1)
    assert tracks == []


def test_two_separate_measurements_two_tracks():
    tm = TrackManager()
    tracks = tm.step(
        [_meas(0.0, 0.0), _meas(500.0, 500.0)], dt=0.1
    )
    assert len(tracks) == 2


def test_same_position_second_tick_updates_track_not_spawn():
    tm = TrackManager()
    tm.step([_meas(50.0, 50.0)], dt=0.1)
    tracks = tm.step([_meas(50.5, 50.3)], dt=0.1)
    assert len(tracks) == 1
    assert tracks[0].hits == 2


def test_sources_merged_from_multi_sensor_hits():
    tm = TrackManager(n_confirm=1)
    tm.step([_meas(0.0, 0.0, SensorType.CAMERA)], dt=0.1)
    tm.step([_meas(0.5, 0.5, SensorType.RF_ODID)], dt=0.1)
    tracks = tm.active_tracks()
    assert len(tracks) == 1
    assert SensorType.CAMERA in tracks[0].sources
    assert SensorType.RF_ODID in tracks[0].sources


def test_uas_id_preserved_from_rf_measurement():
    tm = TrackManager(n_confirm=1)
    tm.step([_meas(0.0, 0.0, SensorType.CAMERA)], dt=0.1)
    m = _meas(0.5, 0.5, SensorType.RF_ODID)
    m.uas_id = "DJI-ABC123"
    tm.step([m], dt=0.1)
    tracks = tm.active_tracks()
    assert tracks[0].uas_id == "DJI-ABC123"
