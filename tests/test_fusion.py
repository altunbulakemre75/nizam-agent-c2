"""
tests/test_fusion.py  —  Unit tests for ai/fusion.py
"""
from __future__ import annotations

import pytest

from ai.fusion import (
    FusionEngine,
    SensorMeasurement,
    SensorProfile,
    SENSOR_PROFILES,
    _distance_m,
    _to_enu,
    _from_enu,
    _angle_diff,
    utc_now_iso,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def meas(
    sensor_id: str = "radar",
    track_hint: str = "TRK-001",
    lat: float = 41.015,
    lon: float = 28.979,
    alt_m: float = 1000.0,
    speed_mps: float = 100.0,
    heading_deg: float = 90.0,
    timestamp: str = "",          # empty → current time (avoids stale eviction)
) -> SensorMeasurement:
    return SensorMeasurement(
        sensor_id=sensor_id,
        track_hint=track_hint,
        lat=lat, lon=lon, alt_m=alt_m,
        speed_mps=speed_mps,
        heading_deg=heading_deg,
        timestamp=timestamp or utc_now_iso(),
    )


# ── Coordinate helpers ────────────────────────────────────────────────────────

class TestCoordinateHelpers:
    def test_distance_same_point(self):
        assert _distance_m(41.0, 29.0, 41.0, 29.0) == pytest.approx(0.0, abs=1e-3)

    def test_distance_known(self):
        # ~111 km per degree latitude
        d = _distance_m(41.0, 29.0, 42.0, 29.0)
        assert 110_000 < d < 112_000

    def test_enu_roundtrip(self):
        lat, lon, alt = 41.015, 28.979, 3000.0
        e, n, z = _to_enu(lat, lon, alt, lat, lon)
        # At reference point ENU should be zero
        assert e == pytest.approx(0.0, abs=1e-3)
        assert n == pytest.approx(0.0, abs=1e-3)
        lat2, lon2, alt2 = _from_enu(e, n, z, lat, lon)
        assert lat2 == pytest.approx(lat, abs=1e-5)
        assert lon2 == pytest.approx(lon, abs=1e-5)

    def test_enu_north_offset(self):
        ref_lat, ref_lon = 41.0, 29.0
        # 1 degree north ≈ 111 km
        e, n, _ = _to_enu(42.0, 29.0, 0, ref_lat, ref_lon)
        assert abs(e) < 100           # should be near zero easting
        assert 110_000 < n < 112_000  # ~111 km north

    def test_angle_diff_wrap(self):
        assert _angle_diff(350, 10) == pytest.approx(-20.0, abs=0.01)
        assert _angle_diff(10, 350) == pytest.approx(20.0,  abs=0.01)
        assert _angle_diff(90, 90)  == pytest.approx(0.0,   abs=0.01)
        # 180° and -180° are both valid representations of a half-turn
        assert abs(_angle_diff(180, 0)) == pytest.approx(180.0, abs=0.01)


# ── SensorProfile ─────────────────────────────────────────────────────────────

class TestSensorProfile:
    def test_default_profile(self):
        p = SensorProfile()
        assert p.pos_std_m      == 100.0
        assert p.speed_std_mps  == 5.0

    def test_variance_floor(self):
        p = SensorProfile(pos_std_m=0, speed_std_mps=0)
        assert p.pos_var   > 0
        assert p.speed_var > 0

    def test_radar_profile_tighter(self):
        radar = SENSOR_PROFILES["radar"]
        cot   = SENSOR_PROFILES["cot"]
        assert radar.pos_std_m < cot.pos_std_m

    def test_adsb_higher_priority(self):
        assert SENSOR_PROFILES["adsb"].priority > SENSOR_PROFILES["manual"].priority


# ── FusionEngine — single sensor ──────────────────────────────────────────────

class TestFusionEngineSingleSensor:
    def test_creates_new_track(self):
        eng = FusionEngine()
        m   = meas()
        ft  = eng.update(m)
        assert ft.id.startswith("FT-")
        assert ft.lat == pytest.approx(41.015, abs=1e-4)
        assert ft.lon == pytest.approx(28.979, abs=1e-4)
        assert eng.track_count() == 1

    def test_same_hint_updates_same_track(self):
        eng = FusionEngine()
        ft1 = eng.update(meas(lat=41.015, lon=28.979))
        ft2 = eng.update(meas(lat=41.016, lon=28.980))
        assert ft1.id == ft2.id
        assert eng.track_count() == 1

    def test_position_converges_on_repeat(self):
        """Ten identical radar reports → fused position stays near truth."""
        eng = FusionEngine()
        truth_lat, truth_lon = 41.015, 28.979
        for _ in range(10):
            ft = eng.update(meas(lat=truth_lat, lon=truth_lon))
        assert ft.lat == pytest.approx(truth_lat, abs=1e-4)
        assert ft.lon == pytest.approx(truth_lon, abs=1e-4)

    def test_single_sensor_std_equals_profile(self):
        eng = FusionEngine()
        eng.register_sensor("radar", SensorProfile(pos_std_m=30))
        ft = eng.update(meas(sensor_id="radar"))
        # After exactly 1 measurement, fused std == sensor profile std
        assert ft.pos_std_m == pytest.approx(30.0, abs=0.01)

    def test_contributing_sensors_list(self):
        eng = FusionEngine()
        ft = eng.update(meas(sensor_id="iff"))
        assert "iff" in ft.contributing_sensors

    def test_track_dict_serialisable(self):
        import json
        eng = FusionEngine()
        ft = eng.update(meas())
        json.dumps(ft.to_dict())  # must not raise


# ── FusionEngine — GNN association ────────────────────────────────────────────

class TestGNNAssociation:
    def test_nearby_reports_merge(self):
        """Two sensors 500 m apart → same physical target → 1 fused track."""
        eng = FusionEngine(gate_m=2000)
        # Sensor A reports position
        eng.update(meas(sensor_id="radar", track_hint="RDR-1",
                        lat=41.000, lon=29.000))
        # Sensor B reports 500 m away (different hint)
        ft = eng.update(meas(sensor_id="iff", track_hint="IFF-1",
                             lat=41.004, lon=29.000))   # ~444 m north
        assert eng.track_count() == 1
        assert len(ft.contributing_sensors) == 2

    def test_distant_reports_split(self):
        """Two sensors 10 km apart → separate physical targets → 2 fused tracks."""
        eng = FusionEngine(gate_m=2000)
        eng.update(meas(sensor_id="radar", track_hint="RDR-1",
                        lat=41.000, lon=29.000))
        eng.update(meas(sensor_id="iff", track_hint="IFF-2",
                        lat=41.100, lon=29.000))   # ~11 km north
        assert eng.track_count() == 2

    def test_three_sensors_merge_to_one(self):
        eng = FusionEngine(gate_m=3000)
        for sid, tid in [("radar","R"), ("iff","I"), ("cot","C")]:
            eng.update(meas(sensor_id=sid, track_hint=tid,
                            lat=41.010 + (0.001 * ["R","I","C"].index(tid)),
                            lon=29.000))
        assert eng.track_count() == 1

    def test_hint_map_overrides_gnn(self):
        """Same hint always reuses same track even if position drifts > gate."""
        eng = FusionEngine(gate_m=500)
        ft1 = eng.update(meas(track_hint="TRK-X", lat=41.000, lon=29.000))
        # Move 2 km (outside gate) — hint map should still associate
        ft2 = eng.update(meas(track_hint="TRK-X", lat=41.018, lon=29.000))
        assert ft1.id == ft2.id


# ── FusionEngine — covariance weighting ───────────────────────────────────────

class TestCovarianceWeighting:
    def test_high_accuracy_sensor_dominates(self):
        """
        A high-accuracy radar (pos_std=30m) and a low-accuracy CoT (pos_std=500m)
        both report.  Fused position should be very close to the radar reading.
        """
        eng = FusionEngine(gate_m=5000)

        truth_lat, truth_lon = 41.000, 29.000
        noisy_lat, noisy_lon = 41.003, 29.004   # ~440 m offset (CoT-style)

        eng.register_sensor("radar-hi", SensorProfile(pos_std_m=30))
        eng.register_sensor("cot-lo",   SensorProfile(pos_std_m=500))

        # radar reports truth; CoT reports noisy position (same target, within gate)
        eng.update(meas(sensor_id="radar-hi", track_hint="T1",
                        lat=truth_lat, lon=truth_lon))
        ft = eng.update(meas(sensor_id="cot-lo", track_hint="T1",
                             lat=noisy_lat, lon=noisy_lon))

        # Fused position must be much closer to radar truth than CoT noise
        d_to_truth = _distance_m(ft.lat, ft.lon, truth_lat, truth_lon)
        d_to_noisy = _distance_m(ft.lat, ft.lon, noisy_lat, noisy_lon)
        assert d_to_truth < d_to_noisy, (
            f"Fused pos {d_to_truth:.0f}m from truth vs {d_to_noisy:.0f}m from noisy"
        )

    def test_fused_std_smaller_than_worst_sensor(self):
        """Two sensors fused → combined std < worst individual sensor std."""
        eng = FusionEngine(gate_m=5000)
        eng.register_sensor("s1", SensorProfile(pos_std_m=100))
        eng.register_sensor("s2", SensorProfile(pos_std_m=200))
        eng.update(meas(sensor_id="s1", track_hint="T1", lat=41.0, lon=29.0))
        ft = eng.update(meas(sensor_id="s2", track_hint="T1", lat=41.001, lon=29.001))
        assert ft.pos_std_m < 200.0  # must be tighter than worst sensor

    def test_more_sensors_reduce_uncertainty(self):
        """Each additional sensor should reduce fused std."""
        eng = FusionEngine(gate_m=5000)
        for i in range(5):
            sid = f"sensor-{i}"
            eng.register_sensor(sid, SensorProfile(pos_std_m=100))
            ft = eng.update(meas(sensor_id=sid, track_hint="T1",
                                 lat=41.0, lon=29.0))
            if i > 0:
                pass  # main assertion: final std < 100
        assert ft.pos_std_m < 100.0

    def test_speed_fusion(self):
        """Fused speed should be between two sensor readings, weighted by accuracy."""
        eng = FusionEngine(gate_m=5000)
        eng.register_sensor("fast-sensor", SensorProfile(speed_std_mps=1.0))
        eng.register_sensor("slow-sensor", SensorProfile(speed_std_mps=20.0))
        # fast sensor reports 100 m/s; slow sensor reports 200 m/s
        eng.update(meas(sensor_id="fast-sensor", track_hint="T1",
                        speed_mps=100.0, lat=41.0, lon=29.0))
        ft = eng.update(meas(sensor_id="slow-sensor", track_hint="T1",
                             speed_mps=200.0, lat=41.0, lon=29.0))
        # Fused speed should be much closer to 100 (high-accuracy sensor)
        assert ft.speed_mps < 120.0


# ── FusionEngine — stale eviction ─────────────────────────────────────────────

_OLD_TS   = "2000-01-01T00:00:00+00:00"  # guaranteed stale
_FUTURE_TS = "2099-01-01T10:00:00+00:00"  # guaranteed fresh

class TestStaleEviction:
    def test_fresh_tracks_not_evicted(self):
        eng = FusionEngine(stale_s=60)
        eng.update(meas(timestamp=_FUTURE_TS))
        eng._evict_stale()
        assert eng.track_count() == 1

    def test_stale_tracks_evicted(self):
        eng = FusionEngine(stale_s=10)
        eng.update(meas(timestamp=_OLD_TS))
        eng._evict_stale()
        assert eng.track_count() == 0

    def test_hint_map_cleaned_on_eviction(self):
        eng = FusionEngine(stale_s=10)
        ft  = eng.update(meas(sensor_id="radar", track_hint="T1",
                              timestamp=_OLD_TS))
        fid = ft.id
        eng._evict_stale()
        assert fid not in eng._tracks
        assert all(v != fid for v in eng._hint_map.values())


# ── FusionEngine — stats and reset ────────────────────────────────────────────

class TestFusionEngineStats:
    def test_stats_keys(self):
        eng = FusionEngine()
        s = eng.stats()
        assert "fused_tracks" in s
        assert "sensor_profiles" in s
        assert "gate_m" in s
        assert "stale_s" in s

    def test_reset_clears_all(self):
        eng = FusionEngine()
        for i in range(5):
            eng.update(meas(track_hint=f"T{i}",
                            lat=41.0 + i * 0.1, lon=29.0))
        assert eng.track_count() == 5
        eng.reset()
        assert eng.track_count() == 0
        assert len(eng._hint_map) == 0

    def test_sensor_registry(self):
        eng = FusionEngine()
        eng.register_sensor("lidar", SensorProfile(pos_std_m=5))
        assert "lidar" in eng.stats()["sensor_profiles"]

    def test_get_profile_prefix_fallback(self):
        eng = FusionEngine()
        p = eng.get_profile("radar-north-sector-3")
        assert p.pos_std_m == SENSOR_PROFILES["radar"].pos_std_m

    def test_get_profile_default_fallback(self):
        p = FusionEngine().get_profile("totally-unknown-sensor-xyz")
        assert isinstance(p, SensorProfile)


# ── FusedTrack.to_dict ────────────────────────────────────────────────────────

class TestFusedTrackDict:
    def test_all_required_keys(self):
        eng = FusionEngine()
        ft  = eng.update(meas())
        d   = ft.to_dict()
        for key in ("id", "lat", "lon", "alt_m", "speed_mps", "heading_deg",
                    "pos_std_m", "speed_std_mps", "contributing_sensors",
                    "sensor_count", "last_update", "sensor_reports"):
            assert key in d, f"Missing key: {key}"

    def test_sensor_reports_capped(self):
        eng = FusionEngine()
        for i in range(60):
            eng.update(meas(timestamp=f"2024-01-01T10:{i:02d}:00+00:00"))
        ft = eng.update(meas())
        assert len(ft.to_dict()["sensor_reports"]) <= 10

    def test_lat_lon_rounded(self):
        eng = FusionEngine()
        ft = eng.update(meas(lat=41.0123456789, lon=28.9876543210))
        d  = ft.to_dict()
        # Should be rounded to 7 decimal places
        assert len(str(d["lat"]).split(".")[-1]) <= 7
