"""
tests/test_blue_force.py — Unit tests for ai/blue_force.py

Covers:
  - No friendly assets → no downgrade
  - Friendly outside corridor → no warning
  - Friendly inside corridor → WEAPONS_FREE downgraded to WEAPONS_HOLD
  - WEAPONS_TIGHT advisory not affected
  - Multiple advisories, only WEAPONS_FREE checked
  - BFT warning contains asset name and clearance
  - Custom lethal_radius_m respected
  - No effector reachable (no friendlies) → infeasible downgrade
"""
from __future__ import annotations

import pytest
from ai.blue_force import check_advisories, BFT_LETHAL_RADIUS_M


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _adv(track_id: str = "T1", eng: str = "WEAPONS_FREE") -> dict:
    return {
        "track_id": track_id,
        "engagement": eng,
        "engagement_level": 5,
        "urgency": "CRITICAL",
        "reasons": ["kill zone"],
        "confidence": 80,
    }


def _track(lat: float = 41.0, lon: float = 29.0) -> dict:
    return {"lat": lat, "lon": lon, "id": "T1"}


def _friendly(name: str, lat: float, lon: float) -> dict:
    return {"type": "friendly", "lat": lat, "lon": lon,
            "status": "active", "name": name}


# ── No friendly assets ────────────────────────────────────────────────────────

class TestNoFriendlies:
    def test_no_assets_no_downgrade(self):
        tracks  = {"T1": _track()}
        assets  = {}
        advs, warns = check_advisories([_adv()], tracks, assets)
        assert len(warns) == 0
        # No effector reachable → infeasible → still downgraded
        assert advs[0]["engagement"] == "WEAPONS_HOLD"

    def test_only_hostile_asset_ignored(self):
        tracks = {"T1": _track()}
        assets = {"A1": {"type": "hostile", "lat": 41.001, "lon": 29.001,
                         "status": "active", "name": "H1"}}
        advs, warns = check_advisories([_adv()], tracks, assets)
        assert len(warns) == 0
        # Still no friendly effector reachable
        assert advs[0]["engagement"] == "WEAPONS_HOLD"


# ── Friendly outside corridor ─────────────────────────────────────────────────

class TestFriendlyOutsideCorridor:
    def test_far_friendly_no_warning(self):
        """Friendly 5 km perpendicular from engagement path → safe."""
        tracks   = {"T1": _track(41.0, 29.0)}
        # Effector proxy at 41.05, 29.0 — path is N–S
        # Friendly is 0.045 deg east ≈ 3.5 km away from the path
        assets   = {
            "E1": _friendly("EFF-1", 41.05, 29.0),      # proxy effector
            "F1": _friendly("FOB-1", 41.025, 29.04),    # far off the path
        }
        advs, warns = check_advisories([_adv()], tracks, assets)
        assert len(warns) == 0
        assert advs[0]["engagement"] == "WEAPONS_FREE"


# ── Friendly inside corridor ──────────────────────────────────────────────────

class TestFriendlyInsideCorridor:
    def test_friendly_on_path_triggers_warning(self):
        """Friendly sits exactly on the engagement line → fratricide risk."""
        tracks  = {"T1": _track(41.0, 29.0)}
        assets  = {
            "E1": _friendly("EFF-1", 41.02, 29.0),   # proxy effector (nearest)
            "F1": _friendly("FOB-1", 41.01, 29.0),   # directly between E1 and T1
        }
        advs, warns = check_advisories([_adv()], tracks, assets,
                                       lethal_radius_m=BFT_LETHAL_RADIUS_M)
        assert len(warns) == 1
        assert warns[0]["track_id"] == "T1"
        assert any(r["asset_name"] == "FOB-1" for r in warns[0]["at_risk"])

    def test_downgraded_to_weapons_hold(self):
        tracks  = {"T1": _track(41.0, 29.0)}
        assets  = {
            "E1": _friendly("EFF-1", 41.02, 29.0),
            "F1": _friendly("FOB-1", 41.01, 29.0),
        }
        advs, warns = check_advisories([_adv()], tracks, assets)
        assert advs[0]["engagement"] == "WEAPONS_HOLD"

    def test_bft_reason_added_to_advisory(self):
        tracks  = {"T1": _track(41.0, 29.0)}
        assets  = {
            "E1": _friendly("EFF-1", 41.02, 29.0),
            "F1": _friendly("FOB-1", 41.01, 29.0),
        }
        advs, _ = check_advisories([_adv()], tracks, assets)
        reasons = advs[0].get("reasons", [])
        assert any("MAVİ KUVVET" in r or "BFT" in r or "koridor" in r.lower() or "FOB" in r
                   for r in reasons)

    def test_bft_flag_on_advisory(self):
        tracks  = {"T1": _track(41.0, 29.0)}
        assets  = {
            "E1": _friendly("EFF-1", 41.02, 29.0),
            "F1": _friendly("FOB-1", 41.01, 29.0),
        }
        advs, _ = check_advisories([_adv()], tracks, assets)
        assert advs[0].get("bft_risk") is True

    def test_custom_radius(self):
        """With a 1 m radius, a slightly off-path asset should be safe."""
        tracks  = {"T1": _track(41.0, 29.0)}
        assets  = {
            "E1": _friendly("EFF-1", 41.02, 29.0),
            "F1": _friendly("FOB-1", 41.01, 29.0005),  # ~40 m off path
        }
        advs, warns = check_advisories([_adv()], tracks, assets, lethal_radius_m=1.0)
        assert len(warns) == 0
        assert advs[0]["engagement"] == "WEAPONS_FREE"


# ── Non-WEAPONS_FREE advisories not touched ───────────────────────────────────

class TestNonWeaponsFree:
    def test_weapons_tight_not_checked(self):
        tracks  = {"T1": _track(41.0, 29.0)}
        assets  = {
            "E1": _friendly("EFF-1", 41.02, 29.0),
            "F1": _friendly("FOB-1", 41.01, 29.0),
        }
        tight_adv = _adv("T1", "WEAPONS_TIGHT")
        advs, warns = check_advisories([tight_adv], tracks, assets)
        assert len(warns) == 0
        assert advs[0]["engagement"] == "WEAPONS_TIGHT"

    def test_mixed_advisories(self):
        """Two advisories: one WEAPONS_FREE (risky), one WEAPONS_TIGHT (safe)."""
        tracks  = {
            "T1": _track(41.0, 29.0),
            "T2": {"lat": 41.1, "lon": 29.1},
        }
        assets  = {
            "E1": _friendly("EFF-1", 41.02, 29.0),
            "F1": _friendly("FOB-1", 41.01, 29.0),
        }
        advs_in = [_adv("T1", "WEAPONS_FREE"), _adv("T2", "WEAPONS_TIGHT")]
        advs_out, warns = check_advisories(advs_in, tracks, assets)
        assert len(advs_out) == 2
        assert len(warns) == 1
        wf_adv = next(a for a in advs_out if a["track_id"] == "T1")
        wt_adv = next(a for a in advs_out if a["track_id"] == "T2")
        assert wf_adv["engagement"] == "WEAPONS_HOLD"
        assert wt_adv["engagement"] == "WEAPONS_TIGHT"


# ── Track position missing ────────────────────────────────────────────────────

class TestMissingPosition:
    def test_track_missing_lat_lon_not_checked(self):
        tracks  = {"T1": {"id": "T1"}}   # no lat/lon
        assets  = {"E1": _friendly("EFF-1", 41.0, 29.0)}
        advs, warns = check_advisories([_adv()], tracks, assets)
        assert len(warns) == 0
        assert advs[0]["engagement"] == "WEAPONS_FREE"
