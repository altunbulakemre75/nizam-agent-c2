"""
ai/confidence.py — Threat Confidence Scoring Engine

Produces a calibrated confidence score (0–100) for each threat by fusing
evidence from four independent signal sources:

  1. ML Probability    (weight 0.50) — RandomForest P(HIGH) from ml_threat.py
  2. Intent Certainty  (weight 0.20) — how certain the intent classification is
  3. Track Quality     (weight 0.20) — observation count and recency
  4. Sensor Corroboration (weight 0.10) — number of independent sensors

EW Penalties (subtracted after weighting):
  GPS_SPOOFING / GPS_SPOOFING_GRADUAL : -25  (position unreliable)
  TRAJECTORY_DEVIATION                : -15  (kinematics suspicious)
  COORDINATED_SPOOF                   : -20  (track may be phantom)
  Any other EW alert for this track   : -10

Final score is clamped to [CONFIDENCE_MIN, 100].

Usage:
    from ai import confidence as ai_confidence

    # Single track (call from tactical task after ML + EW results are ready)
    result = ai_confidence.score(
        track_id  = "T-001",
        track     = track_dict,
        threat    = threat_dict,           # may be None
        ml_pred   = ml_predictions.get("T-001"),   # may be None
        ew_alerts = [list of ew alert dicts for this track],
    )
    # result = {"confidence": 73, "grade": "HIGH", "breakdown": {...}}

    # Batch (annotates threat dicts in-place and returns enriched copy)
    enriched_threats = ai_confidence.score_batch(
        tracks, threats, ml_predictions, ew_alerts_by_track
    )
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

# ── Configuration ─────────────────────────────────────────────────────────────

# Component weights — must sum to 1.0
W_ML      = 0.50
W_INTENT  = 0.20
W_TRACK   = 0.20
W_SENSOR  = 0.10

# Track quality: observation count saturation point
TRACK_OBS_SATURATION = 12   # ≥12 observations → full quality score

# Sensor saturation: ≥3 sensors → full corroboration
SENSOR_SATURATION = 3

# EW penalties (points subtracted from 0-100 score)
_EW_PENALTIES: Dict[str, int] = {
    "GPS_SPOOFING":          25,
    "GPS_SPOOFING_GRADUAL":  25,
    "TRAJECTORY_DEVIATION":  15,
    "COORDINATED_SPOOF":     20,
}
_EW_DEFAULT_PENALTY = 10   # for any other EW alert type

# Floor — we never assign absolute zero; minimum evidence baseline
CONFIDENCE_MIN = 5

# Confidence grade thresholds
GRADE_HIGH   = 70    # ≥ 70 → HIGH (WEAPONS_FREE allowed)
GRADE_MEDIUM = 40    # 40–69 → MEDIUM (WEAPONS_TIGHT max)
# < 40 → LOW (WEAPONS_HOLD max)


# ── Public types ──────────────────────────────────────────────────────────────

ConfidenceResult = Dict[str, Any]
# {
#   "confidence":  int           0–100
#   "grade":       str           "HIGH" | "MEDIUM" | "LOW"
#   "breakdown": {
#     "ml":       float          0–1 (normalised component contribution)
#     "intent":   float
#     "track":    float
#     "sensor":   float
#     "ew_penalty": int
#   }
# }


# ── Core scorer ───────────────────────────────────────────────────────────────

def score(
    track_id: str,
    track: Dict[str, Any],
    threat: Optional[Dict[str, Any]],
    ml_pred: Optional[Dict[str, Any]] = None,
    ew_alerts: Optional[List[Dict[str, Any]]] = None,
) -> ConfidenceResult:
    """
    Compute a confidence score for a single track/threat pair.

    Parameters
    ----------
    track_id  : track identifier (for logging)
    track     : track dict from STATE["tracks"]
    threat    : threat dict from STATE["threats"] (may be None for unscored tracks)
    ml_pred   : output of ai_ml.predict_batch()[track_id] (may be None)
    ew_alerts : list of EW alert dicts whose track_id matches this track
                (GPS_SPOOFING, TRAJECTORY_DEVIATION, etc.)

    Returns
    -------
    ConfidenceResult dict — see module docstring.
    """
    threat = threat or {}
    ew_alerts = ew_alerts or []

    # ── Component 1: ML Probability ───────────────────────────────────────────
    if ml_pred is not None:
        ml_prob = float(ml_pred.get("ml_probability", 0.5))
    else:
        # ML not available — use rule-based threat_level as a proxy
        level = threat.get("threat_level", track.get("threat_level", "LOW"))
        ml_prob = {"HIGH": 0.80, "MEDIUM": 0.50, "LOW": 0.20}.get(level, 0.30)

    # ── Component 2: Intent Certainty ─────────────────────────────────────────
    # intent_conf is stored on the track dict (0-1 float, default 0.5)
    intent_conf = float(
        track.get("intent_conf") or
        track.get("kinematics", {}).get("intent_conf") or
        threat.get("intent_conf") or
        0.50
    )

    # ── Component 3: Track Quality ────────────────────────────────────────────
    # Use observation_count if present, else fall back to history length.
    obs_count = int(
        track.get("observation_count") or
        track.get("obs_count") or
        len(track.get("history", [])) or
        1
    )
    track_quality = min(obs_count / TRACK_OBS_SATURATION, 1.0)

    # ── Component 4: Sensor Corroboration ─────────────────────────────────────
    sensors = track.get("sensors") or track.get("source_sensors") or []
    sensor_count = len(sensors) if isinstance(sensors, list) else int(sensors or 1)
    sensor_score = min(sensor_count / SENSOR_SATURATION, 1.0)

    # ── Weighted sum (maps 0-1 → 0-100) ──────────────────────────────────────
    raw_score = (
        W_ML     * ml_prob     +
        W_INTENT * intent_conf +
        W_TRACK  * track_quality +
        W_SENSOR * sensor_score
    ) * 100.0

    # ── EW Penalties ──────────────────────────────────────────────────────────
    ew_penalty = 0
    for alert in ew_alerts:
        alert_type = alert.get("type", "")
        penalty = _EW_PENALTIES.get(alert_type, _EW_DEFAULT_PENALTY)
        ew_penalty = max(ew_penalty, penalty)   # take worst single penalty

    final_score = int(max(CONFIDENCE_MIN, min(100, raw_score - ew_penalty)))

    grade = (
        "HIGH"   if final_score >= GRADE_HIGH   else
        "MEDIUM" if final_score >= GRADE_MEDIUM else
        "LOW"
    )

    return {
        "confidence": final_score,
        "grade":      grade,
        "breakdown": {
            "ml":        round(ml_prob, 3),
            "intent":    round(intent_conf, 3),
            "track":     round(track_quality, 3),
            "sensor":    round(sensor_score, 3),
            "ew_penalty": ew_penalty,
        },
    }


# ── Batch scorer ──────────────────────────────────────────────────────────────

def score_batch(
    tracks: Dict[str, Dict],
    threats: Dict[str, Dict],
    ml_predictions: Dict[str, Dict],
    ew_alerts: List[Dict[str, Any]],
) -> Dict[str, Dict]:
    """
    Score all tracks and return an enriched threats dict (copy, not in-place).

    The returned dict has the same keys as `threats` plus any track_id with a
    threat_level that appears in `tracks` but not yet in `threats`.  Each entry
    is stamped with `confidence` (int) and `confidence_breakdown` (dict).

    Parameters
    ----------
    tracks         : STATE["tracks"] snapshot
    threats        : STATE["threats"] snapshot
    ml_predictions : result from ai_ml.predict_batch()
    ew_alerts      : flat list of all EW alert dicts from this tactical cycle

    Returns
    -------
    Dict[track_id → enriched threat dict]
    """
    # Build per-track EW index once
    ew_by_track: Dict[str, List[Dict]] = {}
    for alert in ew_alerts:
        tid = alert.get("track_id")
        if tid:
            ew_by_track.setdefault(tid, []).append(alert)

    enriched: Dict[str, Dict] = {}
    ts = _utc_now_iso()

    # Score all tracks that have a threat entry
    for track_id, threat in threats.items():
        track  = tracks.get(track_id, {})
        ml_pred = ml_predictions.get(track_id)
        alerts  = ew_by_track.get(track_id, [])

        result = score(track_id, track, threat, ml_pred, alerts)

        enriched_threat = dict(threat)
        enriched_threat["confidence"]           = result["confidence"]
        enriched_threat["confidence_grade"]     = result["grade"]
        enriched_threat["confidence_breakdown"] = result["breakdown"]
        enriched_threat["confidence_ts"]        = ts
        enriched[track_id] = enriched_threat

    return enriched


# ── Utility ───────────────────────────────────────────────────────────────────

def _utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def grade_label(confidence: int) -> str:
    """Convert a numeric confidence score to a human-readable grade."""
    if confidence >= GRADE_HIGH:
        return "HIGH"
    if confidence >= GRADE_MEDIUM:
        return "MEDIUM"
    return "LOW"
