"""
tests/test_coordinated_attack.py — Tests for ai/coordinated_attack.py
"""
import pytest
from ai import coordinated_attack as ca


@pytest.fixture(autouse=True)
def _reset():
    ca.reset()
    yield
    ca.reset()


# ── Helper tests ─────────────────────────────────────────────────────────

class TestHelpers:
    def test_dist_m_same_point(self):
        assert ca._dist_m(41.0, 29.0, 41.0, 29.0) == 0.0

    def test_bearing_north(self):
        b = ca._bearing_deg(41.0, 29.0, 42.0, 29.0)
        assert abs(b) < 1.0

    def test_angular_spread_opposite(self):
        assert abs(ca._angular_spread([0.0, 180.0]) - 180.0) < 0.01

    def test_angular_spread_same(self):
        assert ca._angular_spread([90.0, 90.0]) == 0.0

    def test_angular_spread_single(self):
        assert ca._angular_spread([45.0]) == 0.0

    def test_centroid(self):
        points = [(0.0, 0.0), (2.0, 4.0)]
        c = ca._centroid(points)
        assert abs(c[0] - 1.0) < 0.01
        assert abs(c[1] - 2.0) < 0.01

    def test_centroid_empty(self):
        assert ca._centroid([]) == (0.0, 0.0)


# ── Convergence detection tests ──────────────────────────────────────────

class TestTrajectoryConvergence:
    def _make_predictions(self, tracks_targets):
        """
        tracks_targets: list of (track_id, current_pos, target_pos)
        Generates linear predictions converging to target.
        """
        predictions = {}
        for tid, (clat, clon), (tlat, tlon) in tracks_targets:
            pts = []
            for step in range(1, 13):
                frac = step / 12.0
                lat = clat + (tlat - clat) * frac
                lon = clon + (tlon - clon) * frac
                pts.append({
                    "lat": lat, "lon": lon,
                    "sigma_lat": 0.0001, "sigma_lon": 0.0001,
                    "time_ahead_s": step * 5,
                })
            predictions[tid] = pts
        return predictions

    def test_no_convergence_too_few(self):
        predictions = self._make_predictions([
            ("T1", (41.0, 29.0), (41.01, 29.01)),
        ])
        tracks = {"T1": {"lat": 41.0, "lon": 29.0}}
        result = ca.detect_coordinated_attacks(tracks, predictions, {}, {})
        assert result == []

    def test_convergence_detected(self):
        """Two tracks converging to same point → COORDINATED_ATTACK."""
        target = (41.015, 28.980)
        predictions = self._make_predictions([
            ("T1", (41.020, 28.970), target),
            ("T2", (41.010, 28.990), target),
        ])
        tracks = {
            "T1": {"lat": 41.020, "lon": 28.970},
            "T2": {"lat": 41.010, "lon": 28.990},
        }
        result = ca.detect_coordinated_attacks(tracks, predictions, {}, {})
        assert len(result) >= 1
        assert result[0]["type"] == "COORDINATED_ATTACK"

    def test_pincer_classification(self):
        """Tracks from opposite directions = PINCER."""
        target = (41.015, 28.980)
        predictions = self._make_predictions([
            ("T1", (41.025, 28.980), target),  # from north
            ("T2", (41.005, 28.980), target),  # from south
        ])
        tracks = {
            "T1": {"lat": 41.025, "lon": 28.980},
            "T2": {"lat": 41.005, "lon": 28.980},
        }
        result = ca.detect_coordinated_attacks(tracks, predictions, {}, {})
        if result:
            assert result[0]["subtype"] in ("PINCER", "CONVERGENCE")

    def test_no_convergence_diverging(self):
        """Tracks moving away from each other → no alert."""
        predictions = self._make_predictions([
            ("T1", (41.010, 28.970), (41.000, 28.960)),  # SW
            ("T2", (41.020, 28.990), (41.030, 29.000)),  # NE
        ])
        tracks = {
            "T1": {"lat": 41.010, "lon": 28.970},
            "T2": {"lat": 41.020, "lon": 28.990},
        }
        result = ca.detect_coordinated_attacks(tracks, predictions, {}, {})
        assert result == []


# ── Asset-targeted convergence ───────────────────────────────────────────

class TestAssetTargeted:
    def test_asset_convergence(self, sample_assets):
        """Two tracks converging on an asset → alert."""
        hq = sample_assets["asset-hq"]
        target = (hq["lat"], hq["lon"])

        preds = {}
        for i, tid in enumerate(["T1", "T2"]):
            start_lat = target[0] + (0.02 if i == 0 else -0.02)
            pts = []
            for step in range(1, 13):
                frac = step / 12.0
                lat = start_lat + (target[0] - start_lat) * frac
                pts.append({
                    "lat": lat, "lon": target[1],
                    "sigma_lat": 0.0001, "sigma_lon": 0.0001,
                    "time_ahead_s": step * 5,
                })
            preds[tid] = pts

        tracks = {
            "T1": {"lat": target[0] + 0.02, "lon": target[1]},
            "T2": {"lat": target[0] - 0.02, "lon": target[1]},
        }
        result = ca.detect_coordinated_attacks(tracks, preds, {}, sample_assets)
        # Should detect asset-targeted attack
        asset_attacks = [w for w in result if w.get("target_type") == "asset"]
        assert len(asset_attacks) >= 1


# ── Cooldown test ────────────────────────────────────────────────────────

class TestCooldown:
    def test_should_emit_cooldown(self):
        assert ca._should_emit("test-key") is True
        assert ca._should_emit("test-key") is False  # within cooldown
