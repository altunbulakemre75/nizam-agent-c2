"""
ai/ml_threat.py  —  ML-based Threat Classification

Replaces / augments the rule-based threat scoring with a trained
RandomForest classifier.  Two modes:

  1) Training:  Extract features from replay recordings, train model,
                save to ai/models/threat_rf.joblib

  2) Inference:  Load trained model, score tracks in real-time,
                 return ML probability + predicted threat level

Feature vector (per track):
  - speed_mps          : current speed
  - closing_speed_mps  : negative radial velocity (approach rate)
  - range_m            : distance from sensor origin
  - altitude_m         : altitude (if available)
  - heading_deg        : heading
  - sensor_count       : number of supporting sensors
  - is_drone           : classification label == drone
  - is_helicopter      : classification label == helicopter
  - intent_attack      : intent == attack
  - intent_recon       : intent == reconnaissance
  - intent_loiter      : intent == loitering
  - intent_conf        : intent confidence
  - acceleration       : speed change between frames (derived)
  - maneuver_rate      : heading change rate (derived)
  - min_asset_dist_m   : distance to nearest friendly asset
  - in_zone            : track inside any zone (0/1)
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

MODEL_DIR = Path(__file__).parent / "models"
MODEL_PATH = MODEL_DIR / "threat_rf.joblib"

DEG_TO_M = 111_320.0

FEATURE_NAMES = [
    "speed_mps", "closing_speed_mps", "range_m", "altitude_m",
    "heading_deg", "sensor_count", "is_drone", "is_helicopter",
    "intent_attack", "intent_recon", "intent_loiter", "intent_conf",
    "acceleration", "maneuver_rate", "min_asset_dist_m", "in_zone",
]

LABEL_MAP = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
LABEL_NAMES = ["LOW", "MEDIUM", "HIGH"]

# ── Distance helper ───────────────────────────────────────────────────────────

def _dist_m(lat1, lon1, lat2, lon2):
    dlat = (lat2 - lat1) * DEG_TO_M
    dlon = (lon2 - lon1) * DEG_TO_M * math.cos(math.radians((lat1 + lat2) / 2))
    return math.sqrt(dlat * dlat + dlon * dlon)


# ── Feature Extraction ────────────────────────────────────────────────────────

def extract_track_features(
    track: Dict[str, Any],
    threat: Optional[Dict[str, Any]] = None,
    assets: Optional[Dict[str, Dict]] = None,
    zones: Optional[Dict[str, Dict]] = None,
    prev_track: Optional[Dict[str, Any]] = None,
    dt: float = 1.0,
) -> np.ndarray:
    """Extract feature vector from a single track + context."""
    kin = track.get("kinematics", {})

    speed = track.get("speed") or kin.get("speed_mps") or 0.0
    closing_speed = max(0.0, -(kin.get("radial_velocity_mps") or 0.0))
    range_m = kin.get("range_m") or 0.0
    altitude = track.get("altitude") or track.get("alt") or 0.0
    heading = track.get("heading") or kin.get("heading_deg") or 0.0

    sensors = track.get("supporting_sensors", [])
    sensor_count = len(sensors) if isinstance(sensors, list) else 0

    cls_label = (track.get("classification", {}).get("label") or "").lower()
    is_drone = 1.0 if cls_label == "drone" else 0.0
    is_helicopter = 1.0 if cls_label == "helicopter" else 0.0

    intent = track.get("intent") or (threat or {}).get("intent") or "unknown"
    intent_conf = track.get("intent_conf") or 0.0
    intent_attack = 1.0 if intent == "attack" else 0.0
    intent_recon = 1.0 if intent == "reconnaissance" else 0.0
    intent_loiter = 1.0 if intent == "loitering" else 0.0

    # Derived: acceleration and maneuver rate
    acceleration = 0.0
    maneuver_rate = 0.0
    if prev_track and dt > 0:
        prev_kin = prev_track.get("kinematics", {})
        prev_speed = prev_track.get("speed") or prev_kin.get("speed_mps") or 0.0
        prev_heading = prev_track.get("heading") or prev_kin.get("heading_deg") or 0.0
        acceleration = (speed - prev_speed) / dt
        dh = abs(heading - prev_heading)
        if dh > 180:
            dh = 360 - dh
        maneuver_rate = dh / dt

    # Distance to nearest friendly asset
    lat = track.get("lat")
    lon = track.get("lon")
    min_asset_dist = 99999.0
    if lat is not None and lon is not None and assets:
        for a in (assets.values() if isinstance(assets, dict) else assets):
            asset = a if isinstance(a, dict) else {}
            if asset.get("type") != "friendly":
                continue
            alat, alon = asset.get("lat"), asset.get("lon")
            if alat is not None and alon is not None:
                d = _dist_m(lat, lon, alat, alon)
                if d < min_asset_dist:
                    min_asset_dist = d

    # In any zone?
    in_zone = 0.0
    if lat is not None and lon is not None and zones:
        for z in (zones.values() if isinstance(zones, dict) else zones):
            zone = z if isinstance(z, dict) else {}
            coords = zone.get("coordinates", [])
            if coords and len(coords) >= 3:
                if _point_in_polygon(lat, lon, coords):
                    in_zone = 1.0
                    break

    return np.array([
        speed, closing_speed, range_m, altitude,
        heading, sensor_count, is_drone, is_helicopter,
        intent_attack, intent_recon, intent_loiter, intent_conf,
        acceleration, maneuver_rate, min_asset_dist, in_zone,
    ], dtype=np.float32)


def _point_in_polygon(lat, lon, coords):
    n = len(coords)
    if n < 3:
        return False
    inside = False
    x, y = lon, lat
    j = n - 1
    for i in range(n):
        xi, yi = coords[i][1], coords[i][0]
        xj, yj = coords[j][1], coords[j][0]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-15) + xi):
            inside = not inside
        j = i
    return inside


# ── Training Data Extraction ─────────────────────────────────────────────────

def extract_training_data(recordings_dir: str = None) -> Tuple[np.ndarray, np.ndarray]:
    """
    Extract feature/label pairs from all replay recordings.
    Label = threat_level from the snapshot (rule-based ground truth).
    Returns (X, y) numpy arrays.
    """
    rec_dir = Path(recordings_dir) if recordings_dir else Path(__file__).parent.parent / "recordings"

    X_all = []
    y_all = []

    for rec_file in sorted(rec_dir.glob("*.jsonl")):
        print(f"  Processing: {rec_file.name}")
        prev_tracks: Dict[str, Dict] = {}

        with open(rec_file, "r", encoding="utf-8") as fh:
            prev_t = 0.0
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if obj.get("meta") or obj.get("footer"):
                    continue

                state = obj.get("state", {})
                elapsed = obj.get("elapsed_s", 0.0)
                dt = max(0.1, elapsed - prev_t) if prev_t > 0 else 1.0
                prev_t = elapsed

                tracks_list = state.get("tracks", [])
                threats_list = state.get("threats", [])
                assets_list = state.get("assets", [])
                zones_list = state.get("zones", [])

                # Build threat lookup
                threat_map = {}
                for th in threats_list:
                    tid = th.get("id") or th.get("track_id") or th.get("global_track_id")
                    if tid:
                        threat_map[str(tid)] = th

                # Build asset/zone dicts for feature extraction
                assets_dict = {}
                for a in assets_list:
                    aid = a.get("id", "")
                    assets_dict[aid] = a
                zones_dict = {}
                for z in zones_list:
                    zid = z.get("id", "")
                    zones_dict[zid] = z

                for track in tracks_list:
                    tid = str(track.get("id") or track.get("track_id") or "")
                    if not tid:
                        continue

                    threat = threat_map.get(tid)
                    level = (threat or {}).get("threat_level") or track.get("threat_level")
                    if not level or level not in LABEL_MAP:
                        continue

                    prev = prev_tracks.get(tid)
                    features = extract_track_features(
                        track, threat, assets_dict, zones_dict, prev, dt
                    )

                    X_all.append(features)
                    y_all.append(LABEL_MAP[level])
                    prev_tracks[tid] = track

    if not X_all:
        raise ValueError("No training data extracted. Run some scenarios first.")

    return np.array(X_all), np.array(y_all)


# ── Training ──────────────────────────────────────────────────────────────────

def train(recordings_dir: str = None) -> Dict[str, Any]:
    """
    Train a RandomForest classifier on replay data.
    Saves model to ai/models/threat_rf.joblib.
    Returns training metrics.
    """
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.model_selection import cross_val_score
    from sklearn.metrics import classification_report
    import joblib

    print("Extracting training data from recordings...")
    X, y = extract_training_data(recordings_dir)
    print(f"  Samples: {len(X)}  Features: {X.shape[1]}")
    print(f"  Class distribution: LOW={sum(y==0)} MEDIUM={sum(y==1)} HIGH={sum(y==2)}")

    # Handle class imbalance with balanced weights
    clf = RandomForestClassifier(
        n_estimators=100,
        max_depth=12,
        min_samples_leaf=5,
        class_weight="balanced",
        random_state=42,
        n_jobs=-1,
    )

    # Cross-validation
    print("Cross-validating...")
    scores = cross_val_score(clf, X, y, cv=5, scoring="accuracy")
    print(f"  CV Accuracy: {scores.mean():.3f} (+/- {scores.std():.3f})")

    # Train on full data
    print("Training final model...")
    clf.fit(X, y)

    # Feature importance
    importances = sorted(
        zip(FEATURE_NAMES, clf.feature_importances_),
        key=lambda x: x[1], reverse=True,
    )
    print("  Top features:")
    for name, imp in importances[:8]:
        print(f"    {name}: {imp:.3f}")

    # Classification report on training data (not ideal but informative)
    y_pred = clf.predict(X)
    report = classification_report(y, y_pred, target_names=LABEL_NAMES, output_dict=True)

    # Save model
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump({
        "model": clf,
        "feature_names": FEATURE_NAMES,
        "label_names": LABEL_NAMES,
        "trained_at": time.time(),
        "samples": len(X),
        "cv_accuracy": float(scores.mean()),
    }, MODEL_PATH)
    print(f"  Model saved: {MODEL_PATH}")

    return {
        "samples": len(X),
        "cv_accuracy": round(float(scores.mean()), 3),
        "cv_std": round(float(scores.std()), 3),
        "feature_importance": {n: round(float(v), 3) for n, v in importances},
        "report": report,
    }


# ── Inference ─────────────────────────────────────────────────────────────────

_model = None
_model_meta = None


def _load_model():
    global _model, _model_meta
    if _model is not None:
        return True
    if not MODEL_PATH.exists():
        return False
    try:
        import joblib
        data = joblib.load(MODEL_PATH)
        _model = data["model"]
        _model_meta = data
        return True
    except Exception:
        return False


def predict_track(
    track: Dict[str, Any],
    threat: Optional[Dict[str, Any]] = None,
    assets: Optional[Dict[str, Dict]] = None,
    zones: Optional[Dict[str, Dict]] = None,
    prev_track: Optional[Dict[str, Any]] = None,
    dt: float = 1.0,
) -> Optional[Dict[str, Any]]:
    """
    Predict threat level for a single track using trained ML model.
    Returns dict with ml_level, ml_probability, ml_probabilities or None if model unavailable.
    """
    if not _load_model():
        return None

    features = extract_track_features(track, threat, assets, zones, prev_track, dt)
    X = features.reshape(1, -1)

    proba = _model.predict_proba(X)[0]
    predicted_class = int(np.argmax(proba))

    return {
        "ml_level": LABEL_NAMES[predicted_class],
        "ml_probability": round(float(proba[predicted_class]), 3),
        "ml_probabilities": {
            LABEL_NAMES[i]: round(float(p), 3) for i, p in enumerate(proba)
        },
    }


def predict_batch(
    tracks: Dict[str, Dict],
    threats: Dict[str, Dict],
    assets: Dict[str, Dict],
    zones: Dict[str, Dict],
    prev_tracks: Optional[Dict[str, Dict]] = None,
    dt: float = 1.0,
) -> Dict[str, Dict[str, Any]]:
    """
    Predict threat level for all tracks. Returns {track_id: prediction}.
    """
    if not _load_model():
        return {}

    results = {}
    prev = prev_tracks or {}

    for tid, track in tracks.items():
        pred = predict_track(
            track, threats.get(tid), assets, zones, prev.get(tid), dt
        )
        if pred:
            results[tid] = pred

    return results


def is_available() -> bool:
    """Check if a trained model is available."""
    return _load_model()


def get_model_info() -> Dict[str, Any]:
    """Get model metadata."""
    if not _load_model():
        return {"available": False}
    return {
        "available": True,
        "samples": _model_meta.get("samples", 0),
        "cv_accuracy": _model_meta.get("cv_accuracy", 0),
        "trained_at": _model_meta.get("trained_at", 0),
        "features": FEATURE_NAMES,
    }


def reset():
    global _model, _model_meta
    _model = None
    _model_meta = None


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Train ML threat classifier from replay data")
    ap.add_argument("--recordings", default=None, help="Recordings directory")
    args = ap.parse_args()

    result = train(args.recordings)
    print("\n=== Training Complete ===")
    print(f"Samples: {result['samples']}")
    print(f"CV Accuracy: {result['cv_accuracy']} (+/- {result['cv_std']})")
