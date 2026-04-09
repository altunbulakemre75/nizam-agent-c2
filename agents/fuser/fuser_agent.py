"""
fuser_agent.py  —  NIZAM multi-sensor fusion agent (Phase 2)

Fuses RADAR + RF + EO detections into tracks with:
- Track history (last 20 polar positions)
- Intent classification: attack / reconnaissance / loitering / unknown
- ML-inspired threat scoring (sigmoid activation, no external deps)
- EO sensor classification hints

Input:  sensor.detection.radar | sensor.detection.rf | sensor.detection.eo  (JSONL stdin)
Output: track.update | threat.assessment  (JSONL stdout)
"""
import argparse
import io
import json
import math
import sys
from typing import Any, Dict, List, Optional, Tuple

# Force UTF-8 output on Windows (avoids cp1254 encode errors)
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from shared.utils import utc_now_iso, wrap_deg, make_envelope


def ang_diff_deg(a: float, b: float) -> float:
    return abs(wrap_deg(a - b))


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

HISTORY_MAX = 20


def classify_intent(history: List[Dict]) -> Tuple[str, float]:
    """
    Rule-based intent classification from polar-coordinate track history.

    Returns (intent, confidence) where intent in:
      attack | reconnaissance | loitering | unknown
    """
    if len(history) < 3:
        return "unknown", 0.3

    ranges = [h["range_m"] for h in history]
    azimuths = [h["az_deg"] for h in history]
    n = len(history)

    # Range dynamics
    range_start = ranges[0]
    range_end = ranges[-1]
    range_change = range_start - range_end   # positive = approaching
    avg_closing_rate = range_change / n       # metres per history step

    # Azimuth dynamics
    az_changes = [ang_diff_deg(azimuths[i], azimuths[i - 1]) for i in range(1, n)]
    avg_az_change = sum(az_changes) / len(az_changes) if az_changes else 0.0
    total_range_change = abs(range_end - range_start)

    # --- Attack: rapid approach ---
    if avg_closing_rate > 20.0:
        return "attack", 0.92
    if avg_closing_rate > 8.0:
        return "attack", 0.75
    if avg_closing_rate > 3.0 and range_end < 500:
        return "attack", 0.65

    # --- Loitering: minimal movement ---
    if total_range_change < 30.0 and avg_az_change < 1.5:
        return "loitering", 0.85
    if total_range_change < 80.0 and avg_az_change < 3.0:
        return "loitering", 0.65

    # --- Reconnaissance: significant azimuth sweep ---
    if avg_az_change > 4.0:
        return "reconnaissance", 0.78
    if avg_az_change > 2.0 and avg_closing_rate < 3.0:
        return "reconnaissance", 0.62

    return "unknown", 0.40


# ---------------------------------------------------------------------------
# ML-inspired threat scoring (pure Python — no external deps)
# ---------------------------------------------------------------------------

def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-20.0, min(20.0, x))))


def ml_threat_score(
    range_m: float,
    radial_velocity_mps: float,
    num_sensors: int,
    intent: str = "unknown",
    tti_s: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Computes a threat score using a sigmoid activation over a feature vector.

    Structurally equivalent to a trained logistic regression; weights are
    hand-tuned to match domain intuition and can be replaced by a fitted model.

    Returns: {score: int[0..100], level: str, probability: float}
    """
    closing = max(0.0, -radial_velocity_mps)   # positive = approaching

    x = -2.5  # bias

    # Closing speed
    x += 0.12 * closing

    # Range (inverse — closer ↔ more dangerous)
    x += 300.0 / max(range_m, 50.0) * 0.5

    # TTI
    if tti_s is not None and tti_s > 0:
        x += 60.0 / max(tti_s, 5.0) * 0.4

    # Multi-sensor confirmation
    x += 0.5 * num_sensors

    # Intent modifier
    x += {"attack": 2.5, "reconnaissance": 0.8, "loitering": 0.3, "unknown": 0.0}.get(intent, 0.0)

    prob = _sigmoid(x)
    score = int(round(prob * 100))

    if score >= 75:
        level = "HIGH"
    elif score >= 45:
        level = "MEDIUM"
    else:
        level = "LOW"

    return {"score": score, "level": level, "probability": round(prob, 3)}


# ---------------------------------------------------------------------------
# Track model
# ---------------------------------------------------------------------------

class Track:
    def __init__(self, track_id: str):
        self.track_id = track_id
        self.last_ts: str = ""
        self.range_m: Optional[float] = None
        self.az_deg: Optional[float] = None
        self.el_deg: Optional[float] = None
        self.radial_velocity_mps: Optional[float] = None
        self.supporting_sensors: Dict[str, int] = {}
        self.evidence: List[str] = []
        # Phase 2: history + intent + classification
        self.history: List[Dict] = []   # [{range_m, az_deg, ts}, ...]
        self.intent: str = "unknown"
        self.intent_conf: float = 0.3
        self.label: str = "drone"
        self.label_conf: float = 0.5

    def touch_sensor(self, s: str, note: str = "") -> None:
        self.supporting_sensors[s] = self.supporting_sensors.get(s, 0) + 1
        if note:
            self.evidence.append(note)

    def num_unique_sensors(self) -> int:
        return len(self.supporting_sensors)

    def add_to_history(self, range_m: float, az_deg: float, ts: str) -> None:
        self.history.append({"range_m": range_m, "az_deg": az_deg, "ts": ts})
        if len(self.history) > HISTORY_MAX:
            self.history.pop(0)
        self.intent, self.intent_conf = classify_intent(self.history)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="NIZAM fusion agent v2: history, intent, ML scoring, EO.")
    ap.add_argument("--instance_id",       default="fuser-01")
    ap.add_argument("--host",              default="dev")
    ap.add_argument("--bearing_gate_deg",  type=float, default=12.0)
    ap.add_argument("--range_gate_m",      type=float, default=250.0)
    ap.add_argument("--track_ttl_s",       type=float, default=5.0)
    args = ap.parse_args()

    source_agent = "fuser-agent"
    tracks: Dict[str, Track] = {}

    def make_track_id(range_m: float, az_deg: float) -> str:
        r_bin = int(range_m // 100)
        a_bin = int((az_deg + 180) // 10)
        return f"T-R{r_bin:03d}-A{a_bin:03d}"

    def find_best_track_by_bearing(bearing_deg: float) -> Optional[Track]:
        best_dist = 1e9
        best_tr: Optional[Track] = None
        for tr in tracks.values():
            if tr.az_deg is None:
                continue
            d = ang_diff_deg(tr.az_deg, bearing_deg)
            if d < best_dist:
                best_dist = d
                best_tr = tr
        if best_tr is not None and best_dist <= args.bearing_gate_deg:
            return best_tr
        return None

    def emit(tr: Track, correlation_id: str, ts: str, last_sources: List[str]) -> None:
        """Emit track.update (always) + threat.assessment (if 2+ sensors)."""
        r   = float(tr.range_m or 0.0)
        vr  = float(tr.radial_velocity_mps or 0.0)
        closing = max(0.0, -vr)
        tti = (r / closing) if closing > 0.5 else None

        ml = ml_threat_score(
            range_m=r,
            radial_velocity_mps=vr,
            num_sensors=tr.num_unique_sensors(),
            intent=tr.intent,
            tti_s=tti,
        )
        score = ml["score"]
        level = ml["level"]

        reasons: List[str] = []
        rules_fired: List[str] = []

        if closing > 5:
            reasons.append(f"Approaching {closing:.1f} m/s")
            rules_fired.append("CLOSING")
        if r < 500:
            reasons.append(f"Range {r:.0f} m (< 500)")
            rules_fired.append("CLOSE_RANGE")
        if tti is not None and tti < 60:
            reasons.append(f"TTI {tti:.0f} s")
            rules_fired.append("LOW_TTI")
        if tr.num_unique_sensors() >= 2:
            reasons.append("Multi-sensor confirmed")
            rules_fired.append("MULTI_SENSOR")

        intent_rule = {
            "attack":        ("INTENT_ATTACK",  f"Intent ATTACK ({tr.intent_conf:.0%})"),
            "reconnaissance":("INTENT_RECON",   f"Intent RECON ({tr.intent_conf:.0%})"),
            "loitering":     ("INTENT_LOITER",  f"Intent LOITER ({tr.intent_conf:.0%})"),
        }.get(tr.intent)
        if intent_rule:
            rules_fired.append(intent_rule[0])
            reasons.append(intent_rule[1])

        reasons.append(f"ML p={ml['probability']} -> {score}/100")

        # ---- track.update ----
        track_payload = {
            "global_track_id": tr.track_id,
            "status": "CONFIRMED" if tr.num_unique_sensors() >= 2 else "TENTATIVE",
            "kinematics": {
                "range_m":             tr.range_m,
                "az_deg":              tr.az_deg,
                "el_deg":              tr.el_deg,
                "radial_velocity_mps": tr.radial_velocity_mps,
            },
            "classification":    {"label": tr.label, "conf": tr.label_conf},
            "supporting_sensors": list(tr.supporting_sensors.keys()),
            "evidence":           tr.evidence[-6:],
            "last_update_sources": last_sources,
            # Phase 2 fields
            "intent":      tr.intent,
            "intent_conf": round(tr.intent_conf, 3),
            "history":     tr.history[-20:],   # polar positions → cop_publisher converts to lat/lon
            # Current threat summary (convenience for UI without waiting for threat.assessment)
            "threat_level": level,
            "threat_score": score,
        }

        print(json.dumps(
            make_envelope("track.update", source_agent, args.instance_id, args.host,
                          correlation_id, track_payload, ts),
            ensure_ascii=False,
        ), flush=True)

        # ---- threat.assessment (2+ sensors only) ----
        if tr.num_unique_sensors() >= 2:
            threat_payload = {
                "global_track_id":    tr.track_id,
                "threat_level":       level,
                "score":              score,
                "tti_s":              None if tti is None else round(tti, 1),
                "rules_fired":        rules_fired,
                "reasons":            reasons,
                "recommended_action": "ALERT" if level in ("HIGH", "MEDIUM") else "OBSERVE",
                "intent":             tr.intent,
                "ml_probability":     ml["probability"],
            }

            print(json.dumps(
                make_envelope("threat.assessment", source_agent, args.instance_id, args.host,
                              correlation_id, threat_payload, ts),
                ensure_ascii=False,
            ), flush=True)

    # ------------------------------------------------------------------ main loop
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        ev = json.loads(line)
        et = ev.get("event_type")
        cid = ev.get("correlation_id", "unknown")
        ts  = ev.get("timestamp", utc_now_iso())

        # ── 1. RADAR ──────────────────────────────────────────────────────────
        if et == "sensor.detection.radar":
            for det in ev.get("payload", {}).get("detections", []):
                r  = det.get("range_m")
                az = det.get("az_deg")
                if r is None or az is None:
                    continue

                tid = make_track_id(float(r), float(az))
                tr  = tracks.setdefault(tid, Track(tid))

                tr.last_ts             = ts
                tr.range_m             = float(r)
                tr.az_deg              = float(az)
                tr.el_deg              = float(det.get("el_deg", 0.0))
                tr.radial_velocity_mps = float(det.get("radial_velocity_mps", 0.0))

                tr.add_to_history(tr.range_m, tr.az_deg, ts)
                tr.touch_sensor("RADAR",
                    f"r={tr.range_m:.0f}m az={tr.az_deg:.1f}° vr={tr.radial_velocity_mps:.1f}m/s intent={tr.intent}")

                emit(tr, cid, ts, ["RADAR"])
            continue

        # ── 2. RF ─────────────────────────────────────────────────────────────
        if et == "sensor.detection.rf":
            rf_dets = ev.get("payload", {}).get("detections", [])
            if not rf_dets:
                continue
            bearing = rf_dets[0].get("bearing_deg")
            conf    = rf_dets[0].get("conf")
            if bearing is None:
                continue

            tr = find_best_track_by_bearing(float(bearing))
            if tr is None:
                continue

            tr.touch_sensor("RF", f"bearing={float(bearing):.1f}° conf={conf}")
            emit(tr, cid, ts, ["RF"])
            continue

        # ── 3. EO / Camera ────────────────────────────────────────────────────
        if et == "sensor.detection.eo":
            eo_dets = ev.get("payload", {}).get("detections", [])
            if not eo_dets:
                continue
            det     = eo_dets[0]
            bearing = det.get("bearing_deg")
            if bearing is None:
                continue

            tr = find_best_track_by_bearing(float(bearing))
            if tr is None:
                continue

            # Update classification from EO visual hint
            cls = det.get("classification_hint", {})
            if cls.get("label"):
                tr.label      = cls["label"]
                tr.label_conf = float(cls.get("conf", 0.7))

            tr.touch_sensor("EO", f"bearing={float(bearing):.1f}° cls={tr.label}({tr.label_conf:.0%})")
            emit(tr, cid, ts, ["EO"])
            continue

        # ignore other event types


if __name__ == "__main__":
    main()
