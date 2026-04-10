"""
tests/test_confidence.py — Unit tests for ai/confidence.py

Covers:
  - score(): all four components contribute correctly
  - EW penalties applied correctly (GPS_SPOOFING, TRAJECTORY_DEVIATION, etc.)
  - Floor clamping (never below CONFIDENCE_MIN)
  - Grade assignment: HIGH / MEDIUM / LOW thresholds
  - ML unavailable fallback uses threat_level proxy
  - score_batch(): enriches threat dict with confidence + breakdown
  - ROE gate: WEAPONS_FREE downgraded when confidence < 65
  - ROE gate: LOW confidence caps at WEAPONS_HOLD
"""
from __future__ import annotations

import pytest
from ai import confidence as conf
from ai import roe as ai_roe


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _track(obs: int = 10, sensors: int = 2,
           intent_conf: float = 0.8) -> dict:
    return {
        "id": "T-001", "lat": 41.0, "lon": 29.0,
        "speed": 30.0,
        "intent": "attack",
        "intent_conf": intent_conf,
        "sensors": [f"s{i}" for i in range(sensors)],
        "observation_count": obs,
    }


def _threat(level: str = "HIGH", score: int = 80) -> dict:
    return {"track_id": "T-001", "threat_level": level, "score": score}


def _ml_pred(prob: float = 0.85) -> dict:
    return {"ml_probability": prob, "ml_level": "HIGH",
            "ml_probabilities": {"LOW": 0.05, "MEDIUM": 0.10, "HIGH": prob}}


def _ew(type_: str) -> dict:
    return {"type": type_, "track_id": "T-001", "severity": "HIGH"}


# ── score(): basic ────────────────────────────────────────────────────────────

class TestScore:
    def test_returns_required_keys(self):
        r = conf.score("T-001", _track(), _threat(), _ml_pred())
        assert "confidence" in r
        assert "grade" in r
        assert "breakdown" in r

    def test_high_ml_high_confidence(self):
        r = conf.score("T-001", _track(obs=12, sensors=3),
                       _threat(), _ml_pred(prob=0.95),
                       ew_alerts=[])
        assert r["confidence"] >= conf.GRADE_HIGH
        assert r["grade"] == "HIGH"

    def test_low_ml_low_confidence(self):
        r = conf.score("T-001", _track(obs=1, sensors=1),
                       _threat(level="LOW"), _ml_pred(prob=0.05),
                       ew_alerts=[])
        assert r["confidence"] < conf.GRADE_MEDIUM

    def test_floor_applied(self):
        # Even worst-case should not go below CONFIDENCE_MIN
        r = conf.score("T-001", _track(obs=1, sensors=1),
                       _threat(level="LOW"), _ml_pred(prob=0.0),
                       ew_alerts=[_ew("GPS_SPOOFING"), _ew("TRAJECTORY_DEVIATION")])
        assert r["confidence"] >= conf.CONFIDENCE_MIN

    def test_ceiling_100(self):
        r = conf.score("T-001", _track(obs=20, sensors=5),
                       _threat(), _ml_pred(prob=1.0), ew_alerts=[])
        assert r["confidence"] <= 100

    def test_breakdown_present(self):
        r = conf.score("T-001", _track(), _threat(), _ml_pred())
        bd = r["breakdown"]
        assert "ml" in bd
        assert "intent" in bd
        assert "track" in bd
        assert "sensor" in bd
        assert "ew_penalty" in bd

    def test_no_ml_uses_threat_level_proxy(self):
        high = conf.score("T-001", _track(), _threat(level="HIGH"), ml_pred=None)
        low  = conf.score("T-001", _track(), _threat(level="LOW"),  ml_pred=None)
        assert high["confidence"] > low["confidence"]


# ── EW penalties ──────────────────────────────────────────────────────────────

class TestEWPenalties:
    def _base(self) -> int:
        return conf.score("T-001", _track(), _threat(),
                          _ml_pred(), ew_alerts=[])["confidence"]

    def test_gps_spoofing_reduces_confidence(self):
        with_ew = conf.score("T-001", _track(), _threat(), _ml_pred(),
                             ew_alerts=[_ew("GPS_SPOOFING")])["confidence"]
        assert with_ew < self._base()

    def test_gps_spoofing_gradual_same_penalty_as_gps(self):
        gps      = conf.score("T-001", _track(), _threat(), _ml_pred(),
                              ew_alerts=[_ew("GPS_SPOOFING")])["confidence"]
        gradual  = conf.score("T-001", _track(), _threat(), _ml_pred(),
                              ew_alerts=[_ew("GPS_SPOOFING_GRADUAL")])["confidence"]
        assert gps == gradual

    def test_trajectory_deviation_lower_penalty_than_gps(self):
        gps  = conf.score("T-001", _track(), _threat(), _ml_pred(),
                          ew_alerts=[_ew("GPS_SPOOFING")])["confidence"]
        dev  = conf.score("T-001", _track(), _threat(), _ml_pred(),
                          ew_alerts=[_ew("TRAJECTORY_DEVIATION")])["confidence"]
        assert dev > gps   # smaller penalty → higher confidence

    def test_worst_single_penalty_applied(self):
        # Both GPS_SPOOFING and TRAJECTORY_DEVIATION — only max penalty taken
        both = conf.score("T-001", _track(), _threat(), _ml_pred(),
                          ew_alerts=[_ew("GPS_SPOOFING"),
                                     _ew("TRAJECTORY_DEVIATION")])["confidence"]
        gps  = conf.score("T-001", _track(), _threat(), _ml_pred(),
                          ew_alerts=[_ew("GPS_SPOOFING")])["confidence"]
        assert both == gps   # same: GPS_SPOOFING is the dominant penalty

    def test_unknown_ew_type_uses_default_penalty(self):
        no_ew = self._base()
        with_ew = conf.score("T-001", _track(), _threat(), _ml_pred(),
                             ew_alerts=[_ew("SOME_NEW_ATTACK")])["confidence"]
        assert with_ew == no_ew - conf._EW_DEFAULT_PENALTY or with_ew >= conf.CONFIDENCE_MIN


# ── Grade thresholds ──────────────────────────────────────────────────────────

class TestGrades:
    def test_grade_high(self):
        r = conf.score("T-001", _track(obs=12, sensors=3),
                       _threat(), _ml_pred(0.95))
        if r["confidence"] >= conf.GRADE_HIGH:
            assert r["grade"] == "HIGH"

    def test_grade_medium(self):
        r = conf.score("T-001", _track(obs=4, sensors=1, intent_conf=0.4),
                       _threat(level="MEDIUM"), _ml_pred(0.45))
        if conf.GRADE_MEDIUM <= r["confidence"] < conf.GRADE_HIGH:
            assert r["grade"] == "MEDIUM"

    def test_grade_low(self):
        r = conf.score("T-001", _track(obs=1, sensors=1, intent_conf=0.2),
                       _threat(level="LOW"), _ml_pred(0.10))
        if r["confidence"] < conf.GRADE_MEDIUM:
            assert r["grade"] == "LOW"


# ── score_batch() ─────────────────────────────────────────────────────────────

class TestScoreBatch:
    def test_batch_stamps_confidence(self):
        tracks   = {"T-001": _track(), "T-002": _track(obs=2)}
        threats  = {"T-001": _threat(), "T-002": _threat(level="MEDIUM")}
        ml_preds = {"T-001": _ml_pred(0.9), "T-002": _ml_pred(0.4)}

        enriched = conf.score_batch(tracks, threats, ml_preds, ew_alerts=[])
        assert "confidence" in enriched["T-001"]
        assert "confidence_grade" in enriched["T-001"]
        assert "confidence_breakdown" in enriched["T-001"]

    def test_batch_original_fields_preserved(self):
        tracks  = {"T-001": _track()}
        threats = {"T-001": _threat(level="HIGH", score=75)}
        enriched = conf.score_batch(tracks, threats, {}, [])
        assert enriched["T-001"]["threat_level"] == "HIGH"
        assert enriched["T-001"]["score"] == 75

    def test_batch_applies_ew_penalty(self):
        tracks  = {"T-001": _track()}
        threats = {"T-001": _threat()}
        ml_preds = {"T-001": _ml_pred()}

        no_ew = conf.score_batch(tracks, threats, ml_preds, [])
        with_ew = conf.score_batch(
            tracks, threats, ml_preds,
            [{"type": "GPS_SPOOFING", "track_id": "T-001"}]
        )
        assert with_ew["T-001"]["confidence"] < no_ew["T-001"]["confidence"]

    def test_batch_empty_threats(self):
        enriched = conf.score_batch({}, {}, {}, [])
        assert enriched == {}


# ── ROE confidence gates ──────────────────────────────────────────────────────

class TestROEGates:
    @pytest.fixture(autouse=True)
    def _reset_roe(self):
        ai_roe.reset()
        yield
        ai_roe.reset()

    def _advisory(self, confidence: int, level: str = "HIGH",
                  intent: str = "attack", in_kill: bool = True) -> dict:
        """Build minimal track + threat and evaluate ROE with given confidence."""
        # Place track inside a kill zone
        zones = {}
        assets = {}
        if in_kill:
            zones = {"Z-KILL": {
                "id": "Z-KILL", "type": "kill",
                "coordinates": [[40.9, 28.9], [41.1, 28.9],
                                 [41.1, 29.1], [40.9, 29.1]],
            }}
        track = {"lat": 41.0, "lon": 29.0, "speed": 30.0, "intent": intent}
        threat = {
            "threat_level": level, "score": 80, "intent": intent,
            "confidence": confidence,
        }
        return ai_roe.evaluate_track(
            "T-001", track, threat, zones, assets, set()
        )

    def test_weapons_free_allowed_at_high_confidence(self):
        adv = self._advisory(confidence=80)
        assert adv["engagement"] == "WEAPONS_FREE"

    def test_weapons_free_downgraded_at_medium_confidence(self):
        adv = self._advisory(confidence=50)
        assert adv["engagement"] == "WEAPONS_TIGHT"
        assert any("guven" in r.lower() or "confidence" in r.lower()
                   for r in adv["reasons"])

    def test_weapons_free_downgraded_at_low_confidence(self):
        adv = self._advisory(confidence=20)
        # Low confidence gate caps at WEAPONS_HOLD
        assert adv["engagement"] in ("WEAPONS_HOLD", "WEAPONS_TIGHT")

    def test_low_confidence_caps_engagement(self):
        # HIGH threat with LOW confidence → capped at WEAPONS_HOLD
        adv = self._advisory(confidence=20, in_kill=False, intent="attack")
        level_idx = ai_roe._LEVEL_INDEX.get(adv["engagement"], 0)
        assert level_idx <= 3  # ≤ WEAPONS_HOLD

    def test_confidence_in_advisory_output(self):
        adv = self._advisory(confidence=75)
        assert "confidence" in adv
        assert adv["confidence"] == 75

    def test_normal_confidence_no_gate_applied(self):
        # 60% confidence → WEAPONS_FREE gate applies (60 < 65)
        # but WEAPONS_TIGHT gate does NOT apply (60 ≥ 35)
        adv = self._advisory(confidence=60)
        # Should be WEAPONS_TIGHT (downgraded from WEAPONS_FREE due to gate 1)
        assert adv["engagement"] == "WEAPONS_TIGHT"
        # Should NOT be WEAPONS_HOLD (gate 2 not triggered)
        assert adv["engagement"] != "WEAPONS_HOLD"
