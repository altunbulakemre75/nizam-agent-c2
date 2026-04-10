"""
ai/drift.py — Model Drift Detection Engine

Monitors the distribution of confidence grades and ML probability scores
over a rolling window. When the current distribution diverges significantly
from the established baseline, a drift alarm is raised.

Drift metric: Population Stability Index (PSI)
  PSI  = Σ (actual_i - expected_i) * ln(actual_i / expected_i)

  PSI < 0.10   → No drift         (model stable)
  PSI 0.10–0.25 → Minor drift     (watch closely)
  PSI ≥ 0.25   → Major drift      (consider retraining)

Tracks:
  • Grade distribution     (HIGH / MEDIUM / LOW buckets)
  • ML probability mean + std
  • Approximate false-positive rate from operator feedback
    (ENGAGE rejected when confidence was HIGH)

Usage:
    from ai import drift as ai_drift

    # Called once per tactical cycle with current enriched threats
    ai_drift.record_batch(enriched_threats, ml_predictions)

    # Called from task approve/reject handlers
    ai_drift.record_feedback(track_id, outcome="false_positive")  # rejected
    ai_drift.record_feedback(track_id, outcome="true_positive")   # approved

    status = ai_drift.status()
    # {
    #   "psi": 0.04,
    #   "drift_level": "none",
    #   "grade_dist": {"HIGH": 0.25, "MEDIUM": 0.45, "LOW": 0.30},
    #   "baseline_dist": {"HIGH": 0.30, "MEDIUM": 0.42, "LOW": 0.28},
    #   "ml_mean": 0.61,  "ml_std": 0.18,
    #   "fp_rate": 0.08,
    #   "observations": 342,
    #   "alert": False,
    # }
"""
from __future__ import annotations

import math
import time
from collections import deque
from typing import Any, Dict, List, Optional

# ── Configuration ──────────────────────────────────────────────────────────────

# Rolling window size for grade / ML observations
WINDOW_SIZE = 2000

# Number of observations required before baseline is locked
BASELINE_WARMUP = 100

# PSI thresholds
PSI_MINOR = 0.10
PSI_MAJOR = 0.25

# Smoothing constant added to each bucket to avoid log(0)
_EPSILON = 1e-6

# ── Internal state ─────────────────────────────────────────────────────────────

# Each entry: {"grade": "HIGH"|"MEDIUM"|"LOW", "ml_prob": float, "ts": float}
_window: deque = deque(maxlen=WINDOW_SIZE)

# Baseline distribution (locked after BASELINE_WARMUP observations)
_baseline: Optional[Dict[str, float]] = None

# Feedback counters: {true_positive, false_positive, true_negative, ...}
_feedback: Dict[str, int] = {
    "true_positive":  0,   # HIGH confidence + ENGAGE approved (correct)
    "false_positive": 0,   # HIGH confidence + ENGAGE rejected (wrong)
}


# ── Core functions ─────────────────────────────────────────────────────────────

def record_batch(
    enriched_threats: Dict[str, Dict[str, Any]],
    ml_predictions:   Dict[str, Dict[str, Any]],
) -> None:
    """
    Record current confidence grades and ML probabilities from a tactical cycle.

    Parameters
    ----------
    enriched_threats : {track_id: threat_dict} with "confidence_grade" set
    ml_predictions   : {track_id: {"ml_probability": float, ...}}
    """
    global _baseline

    now = time.time()
    for tid, threat in enriched_threats.items():
        grade = threat.get("confidence_grade") or threat.get("grade")
        if grade not in ("HIGH", "MEDIUM", "LOW"):
            continue
        ml_prob = None
        ml_pred = ml_predictions.get(tid)
        if ml_pred:
            ml_prob = float(ml_pred.get("ml_probability", 0.5))
        _window.append({"grade": grade, "ml_prob": ml_prob, "ts": now})

    # Lock baseline once warmup observations have been collected
    if _baseline is None and len(_window) >= BASELINE_WARMUP:
        _baseline = _compute_grade_dist(list(_window))


def record_feedback(track_id: str, outcome: str) -> None:  # noqa: ARG001
    """
    Record operator feedback signal.

    outcome : "true_positive"  — ENGAGE approved (model was right)
              "false_positive" — ENGAGE rejected (model was wrong)
              other values ignored
    """
    if outcome in _feedback:
        _feedback[outcome] += 1


def status() -> Dict[str, Any]:
    """Return current drift status dict."""
    items = list(_window)
    n = len(items)

    current_dist = _compute_grade_dist(items) if n > 0 else {"HIGH": 0.0, "MEDIUM": 0.0, "LOW": 0.0}

    # PSI vs baseline
    psi = 0.0
    if _baseline and n >= BASELINE_WARMUP:
        psi = _psi(current_dist, _baseline)

    drift_level = (
        "major" if psi >= PSI_MAJOR else
        "minor" if psi >= PSI_MINOR else
        "none"
    )

    # ML probability stats
    ml_probs = [e["ml_prob"] for e in items if e["ml_prob"] is not None]
    ml_mean = sum(ml_probs) / len(ml_probs) if ml_probs else None
    ml_std  = None
    if ml_probs and len(ml_probs) > 1:
        mean = ml_mean
        ml_std = round(math.sqrt(sum((x - mean) ** 2 for x in ml_probs) / len(ml_probs)), 3)
    if ml_mean is not None:
        ml_mean = round(ml_mean, 3)

    tp = _feedback["true_positive"]
    fp = _feedback["false_positive"]
    fp_rate = round(fp / (tp + fp), 3) if (tp + fp) > 0 else None

    return {
        "psi":           round(psi, 4),
        "drift_level":   drift_level,
        "alert":         drift_level in ("minor", "major"),
        "grade_dist":    current_dist,
        "baseline_dist": dict(_baseline) if _baseline else None,
        "ml_mean":       ml_mean,
        "ml_std":        ml_std,
        "fp_rate":       fp_rate,
        "observations":  n,
        "feedback":      dict(_feedback),
        "window_size":   WINDOW_SIZE,
        "baseline_locked": _baseline is not None,
    }


def reset() -> None:
    """Clear all drift state (called on server reset)."""
    global _baseline
    _window.clear()
    _baseline = None
    _feedback["true_positive"]  = 0
    _feedback["false_positive"] = 0


# ── Helpers ────────────────────────────────────────────────────────────────────

def _compute_grade_dist(items: List[Dict]) -> Dict[str, float]:
    """Return grade distribution as fractions summing to 1.0."""
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for e in items:
        if e["grade"] in counts:
            counts[e["grade"]] += 1
    total = sum(counts.values()) or 1
    return {k: round(v / total, 4) for k, v in counts.items()}


def _psi(actual: Dict[str, float], expected: Dict[str, float]) -> float:
    """
    Population Stability Index between two grade distributions.
    Higher PSI → more drift.
    """
    buckets = ("HIGH", "MEDIUM", "LOW")
    psi = 0.0
    for b in buckets:
        a = actual.get(b, 0.0) + _EPSILON
        e = expected.get(b, 0.0) + _EPSILON
        psi += (a - e) * math.log(a / e)
    return psi
