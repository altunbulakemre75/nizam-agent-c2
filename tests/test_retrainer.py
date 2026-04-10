"""
tests/test_retrainer.py — Online retraining + feature cache

Verifies the F2 upgrade: retrainer now captures the real feature vector the
model saw at decision time (via ml_threat._feature_cache) instead of fabricating
a proxy vector. Legacy records without cached features still go through the
proxy path so old feedback.jsonl entries remain usable.
"""
from __future__ import annotations

import json

import pytest

from ai import ml_threat as ml
from ai import retrainer as rt


@pytest.fixture(autouse=True)
def _isolated_feedback(tmp_path, monkeypatch):
    """Redirect feedback.jsonl to a tmp path and clear all state per test."""
    fb = tmp_path / "feedback.jsonl"
    monkeypatch.setattr(rt, "FEEDBACK_PATH", fb)
    monkeypatch.setattr(rt, "MODEL_DIR", tmp_path)
    rt._feedback_buffer.clear()
    rt._retrain_status.update({
        "last_run_at": None, "last_result": None, "running": False,
        "total_feedback": 0, "since_last_retrain": 0,
    })
    ml.clear_feature_cache()
    yield


class TestFeatureCache:
    def test_empty_cache_returns_none(self):
        assert ml.get_features("nonexistent") is None

    def test_cache_and_retrieve(self):
        vec = [1.0, 2.0, 3.0] + [0.0] * 13
        ml._cache_features("t1", vec)
        assert ml.get_features("t1") == vec

    def test_cache_returns_copy(self):
        ml._cache_features("t1", [1.0] * 16)
        a = ml.get_features("t1")
        a[0] = 999.0
        assert ml.get_features("t1")[0] == 1.0

    def test_cache_bounded_eviction(self, monkeypatch):
        monkeypatch.setattr(ml, "_FEATURE_CACHE_MAX", 3)
        for i in range(5):
            ml._cache_features(f"t{i}", [float(i)] * 16)
        assert ml.get_features("t0") is None
        assert ml.get_features("t1") is None
        assert ml.get_features("t2") is not None
        assert ml.get_features("t4") is not None

    def test_cache_recency_update(self, monkeypatch):
        monkeypatch.setattr(ml, "_FEATURE_CACHE_MAX", 3)
        for i in range(3):
            ml._cache_features(f"t{i}", [float(i)] * 16)
        ml._cache_features("t0", [99.0] * 16)
        ml._cache_features("t3", [3.0] * 16)
        assert ml.get_features("t0") is not None
        assert ml.get_features("t1") is None

    def test_clear_feature_cache(self):
        ml._cache_features("t1", [1.0] * 16)
        ml.clear_feature_cache()
        assert ml.get_features("t1") is None


class TestRecordPersistsRealFeatures:
    def test_record_with_cached_vector_persists_features(self):
        ml._cache_features("abc", [5.0] * 16)
        rt.record("abc", {"ml_level": "HIGH", "ml_probability": 0.9}, "true_positive")

        lines = rt.FEEDBACK_PATH.read_text(encoding="utf-8").strip().splitlines()
        rec = json.loads(lines[0])
        assert rec["track_id"] == "abc"
        assert rec["true_label"] == "HIGH"
        assert "features" in rec
        assert len(rec["features"]) == 16
        assert rec["features"] == [5.0] * 16

    def test_record_without_cached_vector_has_no_features_key(self):
        rt.record("no_cache", {"ml_level": "LOW", "ml_probability": 0.2}, "false_positive")
        lines = rt.FEEDBACK_PATH.read_text(encoding="utf-8").strip().splitlines()
        rec = json.loads(lines[0])
        assert "features" not in rec
        assert rec["true_label"] == "LOW"

    def test_record_unknown_outcome_is_dropped(self):
        rt.record("xyz", None, "garbage")
        assert not rt.FEEDBACK_PATH.exists() or rt.FEEDBACK_PATH.read_text() == ""


@pytest.fixture
def _sandbox_model(tmp_path, monkeypatch):
    """Redirect ml_threat model paths to a tmp dir + stub out replay loader."""
    monkeypatch.setattr(ml, "MODEL_DIR", tmp_path / "models")
    monkeypatch.setattr(ml, "MODEL_PATH", tmp_path / "models" / "threat_rf.joblib")
    monkeypatch.setattr(ml, "extract_training_data",
                        lambda *a, **kw: (_ for _ in ()).throw(Exception("no replay")))
    yield tmp_path


class TestRetrainUsesRealFeatures:
    def test_retrain_prefers_real_features_over_proxy(self, _sandbox_model):
        for i in range(10):
            ml._cache_features(f"hi_{i}", [25.0, 20.0, 1500, 200, 180, 3, 1, 0, 1, 0, 0, 0.9, 2, 0.3, 800, 1])
            rt.record(f"hi_{i}", {"ml_level": "HIGH", "ml_probability": 0.85}, "true_positive")
        for i in range(10):
            ml._cache_features(f"lo_{i}", [2.0, 0, 6000, 2000, 270, 1, 0, 0, 0, 1, 0, 0.25, 0, 0, 5000, 0])
            rt.record(f"lo_{i}", {"ml_level": "HIGH", "ml_probability": 0.6}, "false_positive")

        result = rt.trigger(blocking=True)

        assert result["status"] == "success"
        assert result["feedback_used"] == 20
        assert result["feedback_real"] == 20
        assert result["feedback_proxy"] == 0

    def test_retrain_mixes_real_and_legacy_proxy(self, _sandbox_model):
        ml._cache_features("real_1", [10.0] * 16)
        rt.record("real_1", {"ml_level": "HIGH", "ml_probability": 0.9}, "true_positive")

        rt.record("legacy_1", {"ml_level": "LOW", "ml_probability": 0.1}, "false_positive")
        rt.record("legacy_2", {"ml_level": "LOW", "ml_probability": 0.2}, "false_positive")

        result = rt.trigger(blocking=True)

        assert result["status"] == "success"
        assert result["feedback_real"] == 1
        assert result["feedback_proxy"] == 2
        assert result["feedback_used"] == 3
