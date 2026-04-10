"""
tests/test_nonlethal.py — Unit tests for ai/nonlethal.py

Covers:
  - No assets → no recommendations
  - Non-NL asset (friendly / interceptor) → no recommendation
  - Jammer in range → JAM recommendation
  - Spoofer in range → SPOOF recommendation
  - EW asset in range → EW_SUPPRESS recommendation
  - Out-of-range NL asset → no recommendation
  - Inactive NL asset → no recommendation
  - Score >= KINETIC_ONLY_SCORE (90) → no NL (go kinetic)
  - Score < MIN_SCORE_NL (60) → no NL
  - Non-HIGH/MEDIUM threat level → no NL
  - Non-attack/unknown intent → no NL
  - Multiple NL effectors → one task per action type (closest wins)
  - asset with capability field instead of type
  - No lat/lon on asset → skipped
"""
from __future__ import annotations

import pytest
from ai.nonlethal import recommend, NON_LETHAL_ACTIONS, MIN_SCORE_NL, KINETIC_ONLY_SCORE


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _threat(score=75, level="HIGH", intent="attack", lat=41.0, lon=29.0) -> dict:
    return {
        "score": score, "threat_level": level, "intent": intent,
        "lat": lat, "lon": lon,
    }


def _asset(typ: str, lat: float, lon: float, range_km: float = 5.0,
           status: str = "active", cap: str = "") -> dict:
    d = {"type": typ, "lat": lat, "lon": lon, "range_km": range_km,
         "status": status, "name": f"{typ}-asset"}
    if cap:
        d["capability"] = cap
    return d


# ── No assets ─────────────────────────────────────────────────────────────────

class TestNoAssets:
    def test_empty_assets(self):
        assert recommend("T1", _threat(), {}) == []

    def test_no_nl_capable(self):
        assets = {
            "A1": _asset("friendly", 41.01, 29.01),
            "A2": _asset("interceptor", 41.01, 29.01),
        }
        assert recommend("T1", _threat(), assets) == []


# ── Score / level / intent gates ──────────────────────────────────────────────

class TestScoreGates:
    def test_score_at_kinetic_threshold_no_nl(self):
        """score == KINETIC_ONLY_SCORE → imminent, skip NL."""
        assets = {"J1": _asset("jammer", 41.01, 29.01)}
        result = recommend("T1", _threat(score=KINETIC_ONLY_SCORE), assets)
        assert result == []

    def test_score_above_kinetic_threshold_no_nl(self):
        assets = {"J1": _asset("jammer", 41.01, 29.01)}
        result = recommend("T1", _threat(score=95), assets)
        assert result == []

    def test_score_below_min_no_nl(self):
        assets = {"J1": _asset("jammer", 41.01, 29.01)}
        result = recommend("T1", _threat(score=MIN_SCORE_NL - 1), assets)
        assert result == []

    def test_score_at_min_recommend(self):
        assets = {"J1": _asset("jammer", 41.01, 29.01)}
        result = recommend("T1", _threat(score=MIN_SCORE_NL), assets)
        assert len(result) == 1

    def test_score_just_below_kinetic_recommend(self):
        assets = {"J1": _asset("jammer", 41.01, 29.01)}
        result = recommend("T1", _threat(score=KINETIC_ONLY_SCORE - 1), assets)
        assert len(result) == 1

    def test_low_level_no_nl(self):
        assets = {"J1": _asset("jammer", 41.01, 29.01)}
        result = recommend("T1", _threat(score=75, level="LOW"), assets)
        assert result == []

    def test_reconnaissance_no_nl(self):
        """NL not recommended for reconnaissance intent."""
        assets = {"J1": _asset("jammer", 41.01, 29.01)}
        result = recommend("T1", _threat(score=75, intent="reconnaissance"), assets)
        assert result == []

    def test_loitering_no_nl(self):
        assets = {"J1": _asset("jammer", 41.01, 29.01)}
        result = recommend("T1", _threat(score=75, intent="loitering"), assets)
        assert result == []


# ── Action type mapping ────────────────────────────────────────────────────────

class TestActionTypes:
    def test_jammer_returns_jam(self):
        assets = {"J1": _asset("jammer", 41.01, 29.01)}
        result = recommend("T1", _threat(), assets)
        assert len(result) == 1
        assert result[0]["action"] == "JAM"

    def test_spoofer_returns_spoof(self):
        assets = {"S1": _asset("spoofer", 41.01, 29.01)}
        result = recommend("T1", _threat(), assets)
        assert result[0]["action"] == "SPOOF"

    def test_gps_spoof_cap_returns_spoof(self):
        assets = {"S1": _asset("friendly", 41.01, 29.01, cap="gps_spoof")}
        result = recommend("T1", _threat(), assets)
        assert result[0]["action"] == "SPOOF"

    def test_ew_returns_ew_suppress(self):
        assets = {"E1": _asset("ew", 41.01, 29.01)}
        result = recommend("T1", _threat(), assets)
        assert result[0]["action"] == "EW_SUPPRESS"

    def test_soft_kill_returns_ew_suppress(self):
        assets = {"E1": _asset("soft_kill", 41.01, 29.01)}
        result = recommend("T1", _threat(), assets)
        assert result[0]["action"] == "EW_SUPPRESS"

    def test_capability_field_used(self):
        """NL keyword in 'capability' field, not 'type'."""
        assets = {"E1": _asset("friendly", 41.01, 29.01, cap="electronic")}
        result = recommend("T1", _threat(), assets)
        assert result[0]["action"] == "EW_SUPPRESS"


# ── Range filtering ───────────────────────────────────────────────────────────

class TestRangeFilter:
    def test_out_of_range_not_recommended(self):
        # Threat at 41.0, 29.0 — jammer at ~1.1 km, range only 0.5 km
        assets = {"J1": _asset("jammer", 41.01, 29.01, range_km=0.5)}
        result = recommend("T1", _threat(lat=41.0, lon=29.0), assets)
        assert result == []

    def test_in_range_recommended(self):
        assets = {"J1": _asset("jammer", 41.01, 29.01, range_km=5.0)}
        result = recommend("T1", _threat(lat=41.0, lon=29.0), assets)
        assert len(result) == 1

    def test_asset_no_lat_lon_skipped(self):
        assets = {"J1": {"type": "jammer", "lat": 0.0, "lon": 0.0,
                          "range_km": 5.0, "status": "active", "name": "J1"}}
        result = recommend("T1", _threat(), assets)
        assert result == []


# ── Status filtering ──────────────────────────────────────────────────────────

class TestStatusFilter:
    def test_inactive_asset_skipped(self):
        assets = {"J1": _asset("jammer", 41.01, 29.01, status="inactive")}
        result = recommend("T1", _threat(), assets)
        assert result == []

    def test_offline_asset_skipped(self):
        assets = {"J1": _asset("jammer", 41.01, 29.01, status="offline")}
        result = recommend("T1", _threat(), assets)
        assert result == []


# ── Multiple effectors / deduplication ───────────────────────────────────────

class TestMultipleEffectors:
    def test_two_jammers_one_task(self):
        """Only one JAM task per threat regardless of how many jammers."""
        assets = {
            "J1": _asset("jammer", 41.01, 29.01),
            "J2": _asset("jammer", 41.02, 29.02),
        }
        result = recommend("T1", _threat(lat=41.0, lon=29.0), assets)
        jams = [r for r in result if r["action"] == "JAM"]
        assert len(jams) == 1

    def test_closest_jammer_selected(self):
        """When two jammers in range, the closer one is chosen."""
        assets = {
            "J_near": _asset("jammer", 41.005, 29.005),   # ~0.7 km
            "J_far":  _asset("jammer", 41.02,  29.02),    # ~2.7 km
        }
        result = recommend("T1", _threat(lat=41.0, lon=29.0), assets)
        assert len(result) == 1
        assert result[0]["effector_id"] == "J_near"

    def test_jammer_and_spoofer_two_tasks(self):
        assets = {
            "J1": _asset("jammer", 41.01, 29.01),
            "S1": _asset("spoofer", 41.01, 29.01),
        }
        result = recommend("T1", _threat(), assets)
        actions = {r["action"] for r in result}
        assert "JAM" in actions
        assert "SPOOF" in actions
        assert len(result) == 2

    def test_result_contains_effector_fields(self):
        assets = {"J1": _asset("jammer", 41.01, 29.01)}
        result = recommend("T1", _threat(), assets)
        r = result[0]
        assert "effector_id" in r
        assert "effector_name" in r
        assert "dist_km" in r
        assert r["dist_km"] > 0
