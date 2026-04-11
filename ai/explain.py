"""
ai/explain.py  —  Operator-facing threat explanation

Answers the question: "Why is this track HIGH/MEDIUM/LOW?"

Wraps `ml_threat._model.feature_importances_` + the cached feature vector
for a track and turns them into a short Turkish narrative that an
operator can read at a glance.

Used by GET /api/ai/explain/{track_id}.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ai import ml_threat as _ml


# ── Human-readable feature descriptors ────────────────────────────────────────
# Each entry: (turkish_label, note_formatter, elevated_threshold)
# note_formatter(value) → (note_text, severity: "hot"|"warm"|"cold")

def _fmt_speed(v: float):
    if v >= 150:  return (f"{v:.0f} m/s — çok yüksek hız (füze/roket?)", "hot")
    if v >= 60:   return (f"{v:.0f} m/s — yüksek hız (fixed-wing)", "warm")
    if v >= 15:   return (f"{v:.0f} m/s — normal drone hızı", "cold")
    return (f"{v:.1f} m/s — yavaş/bekleme", "cold")

def _fmt_closing(v: float):
    if v >= 30:   return (f"+{v:.0f} m/s — hızla yaklaşıyor", "hot")
    if v >= 5:    return (f"+{v:.0f} m/s — yaklaşıyor", "warm")
    if v <= -5:   return (f"{v:.0f} m/s — uzaklaşıyor", "cold")
    return ("sabit mesafe", "cold")

def _fmt_range(v: float):
    if v <= 1000: return (f"{v/1000:.1f} km — çok yakın", "hot")
    if v <= 3000: return (f"{v/1000:.1f} km — yakın", "warm")
    return (f"{v/1000:.1f} km — uzak", "cold")

def _fmt_altitude(v: float):
    if v <= 0:    return ("yerde / denizde", "cold")
    if v <= 150:  return (f"{v:.0f} m — çok alçak (radar kaçırma?)", "warm")
    if v >= 3000: return (f"{v:.0f} m — yüksek irtifa", "cold")
    return (f"{v:.0f} m", "cold")

def _fmt_heading(v: float):
    return (f"{v:.0f}°", "cold")

def _fmt_sensors(v: float):
    n = int(v)
    if n >= 3:    return (f"{n} sensör birleştiriyor — güçlü teyit", "warm")
    if n == 2:    return ("2 sensör birleştiriyor", "cold")
    return (f"{n} sensör — zayıf teyit", "cold")

def _fmt_bin(label_if_1: str):
    def _f(v: float):
        return ((label_if_1, "warm") if v >= 0.5 else ("—", "cold"))
    return _f

def _fmt_intent_conf(v: float):
    p = int(round(v * 100))
    if p >= 75:   return (f"%{p} güven", "warm")
    return (f"%{p} güven", "cold")

def _fmt_accel(v: float):
    if v >= 2:    return (f"+{v:.1f} m/s² — hızlanıyor", "warm")
    if v <= -2:   return (f"{v:.1f} m/s² — yavaşlıyor", "cold")
    return ("sabit hız", "cold")

def _fmt_maneuver(v: float):
    if v >= 20:   return (f"{v:.0f}°/s — sert manevra", "hot")
    if v >= 8:    return (f"{v:.0f}°/s — manevra", "warm")
    return ("düz rota", "cold")

def _fmt_min_asset(v: float):
    if v >= 99000: return ("dost birim yok", "cold")
    if v <= 1500:  return (f"{v/1000:.1f} km — dost varlığına çok yakın", "hot")
    if v <= 5000:  return (f"{v/1000:.1f} km — dost bölgesinde", "warm")
    return (f"{v/1000:.1f} km — güvenli mesafe", "cold")

def _fmt_in_zone(v: float):
    return (("tanımlı bölge içinde", "hot") if v >= 0.5 else ("bölge dışı", "cold"))


FEATURE_DESCRIPTORS: Dict[str, Dict[str, Any]] = {
    "speed_mps":         {"label": "Hız",                 "fmt": _fmt_speed},
    "closing_speed_mps": {"label": "Yaklaşma hızı",       "fmt": _fmt_closing},
    "range_m":           {"label": "Menzil",              "fmt": _fmt_range},
    "altitude_m":        {"label": "İrtifa",              "fmt": _fmt_altitude},
    "heading_deg":       {"label": "İstikamet",           "fmt": _fmt_heading},
    "sensor_count":      {"label": "Sensör teyidi",       "fmt": _fmt_sensors},
    "is_drone":          {"label": "Drone sınıfı",        "fmt": _fmt_bin("drone olarak sınıflandırıldı")},
    "is_helicopter":     {"label": "Helikopter sınıfı",   "fmt": _fmt_bin("helikopter olarak sınıflandırıldı")},
    "is_missile":        {"label": "Füze sınıfı",         "fmt": _fmt_bin("FÜZE olarak sınıflandırıldı")},
    "is_fixed_wing":     {"label": "Sabit kanat",         "fmt": _fmt_bin("sabit kanat olarak sınıflandırıldı")},
    "is_vessel":         {"label": "Gemi",                "fmt": _fmt_bin("gemi olarak sınıflandırıldı")},
    "intent_attack":     {"label": "Saldırı niyeti",      "fmt": _fmt_bin("SALDIRI niyeti tespit edildi")},
    "intent_recon":      {"label": "Keşif niyeti",        "fmt": _fmt_bin("keşif niyeti")},
    "intent_loiter":     {"label": "Bekleme niyeti",      "fmt": _fmt_bin("hedef üstünde bekliyor")},
    "intent_conf":       {"label": "Niyet güveni",        "fmt": _fmt_intent_conf},
    "acceleration":      {"label": "İvme",                "fmt": _fmt_accel},
    "maneuver_rate":     {"label": "Manevra oranı",       "fmt": _fmt_maneuver},
    "min_asset_dist_m":  {"label": "Dost birime mesafe",  "fmt": _fmt_min_asset},
    "in_zone":           {"label": "Bölge durumu",        "fmt": _fmt_in_zone},
}


def _get_feature_importances() -> Optional[Dict[str, float]]:
    """Read feature importances from the loaded model, if sklearn is available."""
    if not _ml._load_model() or _ml._model is None:
        return None
    try:
        arr = _ml._model.feature_importances_
    except Exception:
        return None
    return {name: float(arr[i]) for i, name in enumerate(_ml.FEATURE_NAMES) if i < len(arr)}


ENTITY_TYPE_LABELS = {
    "drone":      ("Drone / UAV",       "warm"),
    "helicopter": ("Helikopter",        "warm"),
    "fixed_wing": ("Sabit kanat ucak",  "warm"),
    "missile":    ("FUZE / ROKET",      "hot"),
    "vessel":     ("Gemi / Tekne",      "cold"),
    "vehicle":    ("Kara araci",        "warm"),
    "bird":       ("Kus (hatali alarm)", "cold"),
    "balloon":    ("Balon",             "cold"),
    "unknown":    ("Bilinmiyor",        "cold"),
}


def _entity_type_row(track: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Build a top-row descriptor for the track's entity type."""
    if not track:
        return None
    cls = track.get("classification") or {}
    label = (cls.get("label") or "").lower()
    if not label:
        return None
    display, severity = ENTITY_TYPE_LABELS.get(label, (label.title(), "cold"))
    conf = cls.get("conf")
    conf_txt = f" (%{int(round(conf * 100))} guven)" if isinstance(conf, (int, float)) else ""
    callsign = cls.get("callsign")
    extra = f" — {callsign}" if callsign else ""
    return {
        "name": "entity_type",
        "label": "Varlik tipi",
        "value": label,
        "note": f"{display}{conf_txt}{extra}",
        "severity": severity,
        "importance": 1.0,  # always shown first
    }


def explain_track(
    track_id: str,
    ml_prediction: Optional[Dict[str, Any]] = None,
    track: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build an operator-facing explanation for why a track scored the way it did.

    Parameters
    ----------
    track_id       : track identifier
    ml_prediction  : current ML prediction dict (ml_level, ml_probability, ...)
                     — pulled from AI_ML_PREDICTIONS in the server

    Returns
    -------
    dict with:
      track_id, ml_level, ml_probability, ml_probabilities,
      top_features   : [{name, label, value, note, severity, importance}, ...]
      summary        : short Turkish sentence
      model_available: bool
    """
    features = _ml.get_features(track_id)
    importances = _get_feature_importances()

    # Entity type row — always shown first, even if the ML model is cold
    entity_row = _entity_type_row(track)

    if features is None or importances is None:
        return {
            "track_id": track_id,
            "model_available": False,
            "reason": "No cached feature vector or trained model for this track yet.",
            "ml_level": (ml_prediction or {}).get("ml_level"),
            "ml_probability": (ml_prediction or {}).get("ml_probability"),
            "top_features": [entity_row] if entity_row else [],
            "summary": (entity_row["note"] if entity_row
                        else "Bu iz için ML tahmini henüz hazır değil."),
        }

    # Pair values with feature names
    feature_names = _ml.FEATURE_NAMES
    rows: List[Dict[str, Any]] = []
    for i, name in enumerate(feature_names):
        if i >= len(features):
            continue
        value = float(features[i])
        importance = importances.get(name, 0.0)
        desc = FEATURE_DESCRIPTORS.get(name, {})
        label = desc.get("label", name)
        fmt = desc.get("fmt")
        if fmt:
            note, severity = fmt(value)
        else:
            note, severity = (f"{value:.2f}", "cold")

        # Skip features that don't contribute meaningful signal (cold binary-off)
        is_binary = name.startswith("is_") or name.startswith("intent_") and name != "intent_conf" or name == "in_zone"
        if is_binary and value < 0.5 and severity == "cold":
            continue

        rows.append({
            "name": name,
            "label": label,
            "value": round(value, 2),
            "note": note,
            "severity": severity,
            "importance": round(importance, 3),
        })

    # Sort: hot first, then warm, then by importance desc
    severity_rank = {"hot": 0, "warm": 1, "cold": 2}
    rows.sort(key=lambda r: (severity_rank.get(r["severity"], 3), -r["importance"]))
    top = rows[:6]

    # Prepend entity type row so the operator sees *what* before *why*
    if entity_row:
        top = [entity_row] + top

    # Build a 1-sentence summary from the top hot/warm features
    hot_bits = [r["label"] + " — " + r["note"] for r in top if r["severity"] in ("hot", "warm")][:3]
    ml_level = (ml_prediction or {}).get("ml_level") or "?"
    ml_prob = (ml_prediction or {}).get("ml_probability")
    prob_pct = f"%{int(ml_prob * 100)}" if isinstance(ml_prob, (int, float)) else ""

    if hot_bits:
        summary = f"{', '.join(hot_bits)} → {prob_pct} olasılıkla {ml_level}"
    else:
        summary = f"Belirgin risk göstergesi yok → {prob_pct} {ml_level}"

    return {
        "track_id": track_id,
        "model_available": True,
        "ml_level": (ml_prediction or {}).get("ml_level"),
        "ml_probability": (ml_prediction or {}).get("ml_probability"),
        "ml_probabilities": (ml_prediction or {}).get("ml_probabilities"),
        "top_features": top,
        "summary": summary,
    }
