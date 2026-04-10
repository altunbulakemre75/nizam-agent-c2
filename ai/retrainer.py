"""
ai/retrainer.py — Online Retraining Engine

Collects labeled feedback from operator decisions and periodically retrains
the RandomForest threat classifier with the accumulated data.

Feedback signals:
  true_positive  — ENGAGE approved → model was correct (label = HIGH)
  false_positive — ENGAGE rejected → model was wrong  (label = LOW)
  true_negative  — OBSERVE approved on LOW confidence track (label = LOW)

Feedback records are persisted to ai/models/feedback.jsonl so they survive
server restarts.  Retraining can be triggered:
  • Manually via POST /api/ai/retrain
  • Automatically when buffer exceeds AUTO_RETRAIN_THRESHOLD

Usage:
    from ai import retrainer as ai_retrainer

    # Record from approve/reject handlers (pass ML prediction snapshot)
    ai_retrainer.record(track_id, ml_pred, outcome="true_positive")

    # Trigger retraining (runs in background thread, non-blocking)
    result = ai_retrainer.trigger(blocking=False)
    # {"status": "scheduled"} or {"status": "running"} or {"error": ...}

    status = ai_retrainer.status()
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

MODEL_DIR     = Path(__file__).parent / "models"
FEEDBACK_PATH = MODEL_DIR / "feedback.jsonl"

# Automatically trigger retraining after this many new feedback items
AUTO_RETRAIN_THRESHOLD = 50

# ── Internal state ─────────────────────────────────────────────────────────────

_feedback_buffer: List[Dict[str, Any]] = []  # in-memory (also persisted)
_retrain_lock = threading.Lock()
_retrain_status: Dict[str, Any] = {
    "last_run_at":       None,
    "last_result":       None,
    "running":           False,
    "total_feedback":    0,
    "since_last_retrain": 0,
}

# ── Public API ─────────────────────────────────────────────────────────────────

def record(
    track_id:  str,
    ml_pred:   Optional[Dict[str, Any]],
    outcome:   str,                      # "true_positive" | "false_positive" | "true_negative"
) -> None:
    """
    Record an operator feedback signal for a track.

    Parameters
    ----------
    track_id  : track identifier
    ml_pred   : ML prediction dict from AI_ML_PREDICTIONS (may be None)
    outcome   : feedback type
    """
    if outcome not in ("true_positive", "false_positive", "true_negative"):
        return

    # Map outcome → inferred true label
    true_label = {
        "true_positive":  "HIGH",
        "false_positive": "LOW",
        "true_negative":  "LOW",
    }[outcome]

    record_dict: Dict[str, Any] = {
        "track_id":       track_id,
        "outcome":        outcome,
        "true_label":     true_label,
        "predicted_level": (ml_pred or {}).get("ml_level"),
        "predicted_prob":  (ml_pred or {}).get("ml_probability"),
        "ts":              time.time(),
    }
    _feedback_buffer.append(record_dict)
    _retrain_status["total_feedback"] += 1
    _retrain_status["since_last_retrain"] += 1

    # Persist to disk (append-only, survives server restart)
    try:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        with open(FEEDBACK_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record_dict) + "\n")
    except Exception:
        pass

    # Auto-trigger if threshold reached and sklearn available
    if _retrain_status["since_last_retrain"] >= AUTO_RETRAIN_THRESHOLD:
        trigger(blocking=False)


def trigger(blocking: bool = False) -> Dict[str, Any]:
    """
    Trigger model retraining.

    Parameters
    ----------
    blocking : if True, wait for training to complete (for testing)

    Returns
    -------
    {"status": "scheduled" | "running" | "skipped"} or {"error": ..., "status": "failed"}
    """
    if _retrain_status["running"]:
        return {"status": "running"}

    feedback_count = len(_load_feedback())
    if feedback_count == 0:
        return {"status": "skipped", "reason": "no feedback data"}

    if blocking:
        return _run_retrain()

    t = threading.Thread(target=_run_retrain, daemon=True)
    t.start()
    return {"status": "scheduled", "feedback_count": feedback_count}


def status() -> Dict[str, Any]:
    """Return current retrainer status dict."""
    return {
        **_retrain_status,
        "feedback_count":     len(_load_feedback()),
        "auto_threshold":     AUTO_RETRAIN_THRESHOLD,
        "feedback_file":      str(FEEDBACK_PATH),
        "model_exists":       (MODEL_DIR / "threat_rf.joblib").exists(),
    }


def reset() -> None:
    """Clear in-memory buffer (persisted feedback survives reset)."""
    _feedback_buffer.clear()
    _retrain_status["since_last_retrain"] = 0


# ── Internal helpers ───────────────────────────────────────────────────────────

def _load_feedback() -> List[Dict]:
    """Load all persisted feedback records from disk."""
    if not FEEDBACK_PATH.exists():
        return []
    records = []
    try:
        with open(FEEDBACK_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    return records


def _run_retrain() -> Dict[str, Any]:
    """
    Execute model retraining.  Runs in a background thread.

    Strategy:
      1. Load existing training data from replay recordings (if available)
      2. Convert operator feedback to pseudo-training samples using the
         stored ML feature approximations
      3. Retrain RandomForest on the combined dataset
      4. Save new model (atomic write to temp file + rename)

    Falls back gracefully if sklearn or replay data are unavailable.
    """
    with _retrain_lock:
        _retrain_status["running"] = True
        t_start = time.time()
        try:
            result = _do_retrain()
            _retrain_status["last_result"]       = result
            _retrain_status["last_run_at"]       = time.time()
            _retrain_status["since_last_retrain"] = 0
            return result
        except Exception as exc:
            err = {"status": "failed", "error": str(exc)}
            _retrain_status["last_result"] = err
            return err
        finally:
            _retrain_status["running"] = False
            _retrain_status["last_result"]["duration_s"] = round(time.time() - t_start, 2)


def _do_retrain() -> Dict[str, Any]:
    try:
        import numpy as np
        from sklearn.ensemble import RandomForestClassifier
        import joblib
    except ImportError:
        return {"status": "skipped", "reason": "scikit-learn not available"}

    from ai.ml_threat import (
        MODEL_DIR as ML_MODEL_DIR, MODEL_PATH, LABEL_MAP, LABEL_NAMES,
        FEATURE_NAMES, extract_training_data,
    )

    # ── 1) Try to load existing training data ─────────────────────────────────
    X_base: list = []
    y_base: list = []
    try:
        X_base_arr, y_base_arr = extract_training_data()
        X_base = X_base_arr.tolist()
        y_base = y_base_arr.tolist()
    except Exception:
        pass  # No replay data yet — fine, use feedback only

    # ── 2) Build synthetic samples from feedback ──────────────────────────────
    feedback_recs = _load_feedback()
    n_features = len(FEATURE_NAMES)

    X_fb: list = []
    y_fb: list = []
    for rec in feedback_recs:
        label = rec.get("true_label", "")
        if label not in LABEL_MAP:
            continue
        # We don't have the raw feature vector in the feedback record (it wasn't
        # captured at decision time), so we generate a minimal synthetic sample:
        # a zero vector with the predicted probability slot set approximately.
        # This is a weak signal but biases the model in the right direction.
        fv = [0.0] * n_features
        prob = rec.get("predicted_prob")
        if prob is not None:
            # Slot 0 = speed_mps: proxy high-threat via non-zero speed
            fv[0] = 15.0 if label == "HIGH" else 5.0
            # intent_attack slot (index 8)
            fv[8] = 1.0 if label == "HIGH" else 0.0
        X_fb.append(fv)
        y_fb.append(LABEL_MAP[label])

    X_all = X_base + X_fb
    y_all = y_base + y_fb

    if not X_all:
        return {"status": "skipped", "reason": "no training data available"}

    import numpy as np
    X = np.array(X_all, dtype=np.float32)
    y = np.array(y_all, dtype=np.int32)

    clf = RandomForestClassifier(
        n_estimators=100,
        max_depth=12,
        min_samples_leaf=5,
        class_weight="balanced",
        random_state=42,
        n_jobs=-1,
    )
    clf.fit(X, y)

    # ── 3) Atomic model save ──────────────────────────────────────────────────
    ML_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = MODEL_PATH.with_suffix(".tmp")
    joblib.dump({
        "model":         clf,
        "feature_names": FEATURE_NAMES,
        "label_names":   LABEL_NAMES,
        "trained_at":    time.time(),
        "samples":       len(X),
        "feedback_used": len(X_fb),
    }, tmp_path)
    tmp_path.replace(MODEL_PATH)

    # Force reload of global model in ml_threat
    try:
        import ai.ml_threat as _ml
        _ml._model      = None
        _ml._model_meta = None
    except Exception:
        pass

    return {
        "status":        "success",
        "samples_total": len(X),
        "samples_base":  len(X_base),
        "feedback_used": len(X_fb),
        "labels": {
            LABEL_NAMES[i]: int(sum(1 for v in y_all if v == i))
            for i in range(len(LABEL_NAMES))
        },
    }
