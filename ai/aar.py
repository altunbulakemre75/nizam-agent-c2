"""
ai/aar.py  —  After-Action Report (AAR) Generator

Collects cumulative statistics throughout a scenario and generates
a structured post-scenario summary report:

  - Executive summary (duration, track counts, peak threat)
  - Threat analysis (distribution by level, peak moments, intent breakdown)
  - Track-by-track analysis (highest threat tracks, intent changes)
  - Anomaly statistics (by type and severity)
  - Coordinated attack log
  - Zone breach log
  - Task/engagement summary
  - Key event timeline (ordered by time)

Used by frontend to render a full AAR modal at any point during
or after a scenario.
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple


# ── Session State ──────────────────────────────────────────────────────────

_session: Dict[str, Any] = {
    "start_time": None,
    "end_time": None,
    "active": False,
}

# Counters
_track_ids_seen: set = set()
_threat_events: List[Dict] = []          # all threat snapshots
_anomaly_events: List[Dict] = []         # all anomalies
_coord_attack_events: List[Dict] = []    # coordinated attack warnings
_zone_breach_events: List[Dict] = []     # zone breach alerts
_task_events: List[Dict] = []            # tasks created
_key_events: List[Dict] = []             # important moments (ordered)

_peak_threat_score: int = 0
_peak_threat_track: str = ""
_peak_threat_time: float = 0.0

_max_concurrent_tracks: int = 0

# Limits
_MAX_EVENTS = 500
_MAX_KEY_EVENTS = 100


# ── Session Control ────────────────────────────────────────────────────────

def start_session() -> None:
    """Begin a new AAR tracking session."""
    reset()
    _session["start_time"] = time.time()
    _session["active"] = True
    _add_key_event("SESSION_START", "Senaryo baslatildi", severity="INFO")


def end_session() -> None:
    """End the current AAR session."""
    _session["end_time"] = time.time()
    _session["active"] = False
    _add_key_event("SESSION_END", "Senaryo sonlandirildi", severity="INFO")


def is_active() -> bool:
    return _session["active"]


# ── Recording API ──────────────────────────────────────────────────────────

def record_track(track_id: str, current_track_count: int) -> None:
    """Record a track observation."""
    global _max_concurrent_tracks
    _track_ids_seen.add(track_id)
    if current_track_count > _max_concurrent_tracks:
        _max_concurrent_tracks = current_track_count


def record_threat(track_id: str, score: int, level: str, intent: str) -> None:
    """Record a threat assessment event."""
    global _peak_threat_score, _peak_threat_track, _peak_threat_time
    now = time.time()
    entry = {
        "track_id": track_id,
        "score": score,
        "level": level,
        "intent": intent,
        "t": now,
    }
    _threat_events.append(entry)
    if len(_threat_events) > _MAX_EVENTS:
        del _threat_events[:len(_threat_events) - _MAX_EVENTS]

    if score > _peak_threat_score:
        _peak_threat_score = score
        _peak_threat_track = track_id
        _peak_threat_time = now

    # Key event for HIGH threats
    if level == "HIGH" and score >= 70:
        _add_key_event(
            "HIGH_THREAT",
            f"Yuksek tehdit: {track_id} (skor:{score}, niyet:{intent})",
            severity="HIGH",
            track_id=track_id,
        )


def record_anomaly(anomaly: Dict[str, Any]) -> None:
    """Record an anomaly detection event."""
    _anomaly_events.append({
        "type": anomaly.get("type", "UNKNOWN"),
        "severity": anomaly.get("severity", "MEDIUM"),
        "track_id": anomaly.get("track_id", ""),
        "track_ids": anomaly.get("track_ids", []),
        "detail": anomaly.get("detail", anomaly.get("message", "")),
        "t": anomaly.get("time", time.time()),
    })
    if len(_anomaly_events) > _MAX_EVENTS:
        del _anomaly_events[:len(_anomaly_events) - _MAX_EVENTS]

    sev = anomaly.get("severity", "MEDIUM")
    if sev in ("CRITICAL", "HIGH"):
        _add_key_event(
            "ANOMALY",
            f"Anomali: {anomaly.get('type', '?')} — {anomaly.get('detail', anomaly.get('message', ''))}",
            severity=sev,
            track_id=anomaly.get("track_id", ""),
        )


def record_coord_attack(attack: Dict[str, Any]) -> None:
    """Record a coordinated attack detection."""
    _coord_attack_events.append({
        "subtype": attack.get("subtype", "CONVERGENCE"),
        "severity": attack.get("severity", "HIGH"),
        "track_ids": attack.get("track_ids", []),
        "count": attack.get("count", 0),
        "time_to_convergence_s": attack.get("time_to_convergence_s", 0),
        "angular_spread_deg": attack.get("angular_spread_deg", 0),
        "target_name": attack.get("target_name", ""),
        "message": attack.get("message", ""),
        "t": attack.get("time", time.time()),
    })
    if len(_coord_attack_events) > _MAX_EVENTS:
        del _coord_attack_events[:len(_coord_attack_events) - _MAX_EVENTS]

    _add_key_event(
        "COORD_ATTACK",
        attack.get("message", f"Koordineli saldiri: {attack.get('subtype', '?')}"),
        severity=attack.get("severity", "HIGH"),
    )


def record_zone_breach(breach: Dict[str, Any]) -> None:
    """Record a zone breach alert."""
    _zone_breach_events.append({
        "track_id": breach.get("track_id", ""),
        "zone_id": breach.get("zone_id", ""),
        "zone_name": breach.get("zone_name", ""),
        "zone_type": breach.get("zone_type", ""),
        "t": time.time(),
    })
    if len(_zone_breach_events) > _MAX_EVENTS:
        del _zone_breach_events[:len(_zone_breach_events) - _MAX_EVENTS]

    _add_key_event(
        "ZONE_BREACH",
        f"Bolge ihlali: {breach.get('track_id', '?')} → {breach.get('zone_name', '?')} ({breach.get('zone_type', '?')})",
        severity="HIGH",
        track_id=breach.get("track_id", ""),
    )


def record_task(task: Dict[str, Any]) -> None:
    """Record a task creation."""
    _task_events.append({
        "id": task.get("id", ""),
        "track_id": task.get("track_id", ""),
        "action": task.get("action", ""),
        "threat_level": task.get("threat_level", ""),
        "status": task.get("status", "PENDING"),
        "t": time.time(),
    })
    if len(_task_events) > _MAX_EVENTS:
        del _task_events[:len(_task_events) - _MAX_EVENTS]


# ── Key Events ─────────────────────────────────────────────────────────────

def _add_key_event(
    event_type: str,
    message: str,
    severity: str = "INFO",
    track_id: str = "",
) -> None:
    """Add a key event to the timeline."""
    # Avoid duplicate messages within 5 seconds
    now = time.time()
    if _key_events:
        last = _key_events[-1]
        if (last["type"] == event_type and
            last.get("track_id") == track_id and
            now - last["t"] < 5.0):
            return

    _key_events.append({
        "type": event_type,
        "message": message,
        "severity": severity,
        "track_id": track_id,
        "t": now,
    })
    if len(_key_events) > _MAX_KEY_EVENTS:
        del _key_events[:len(_key_events) - _MAX_KEY_EVENTS]


# ── Report Generation ─────────────────────────────────────────────────────

def generate_report(
    tracks: Dict[str, Dict],
    threats: Dict[str, Dict],
    zones: Dict[str, Dict],
    assets: Dict[str, Dict],
    tasks: Dict[str, Dict],
    timelines: Optional[Dict[str, List]] = None,
) -> Dict[str, Any]:
    """Generate a complete After-Action Report."""
    now = time.time()
    start = _session.get("start_time") or now
    end = _session.get("end_time") or now
    duration_s = round(end - start, 1)

    # ── Executive Summary ──
    executive = {
        "duration_s": duration_s,
        "duration_display": _format_duration(duration_s),
        "total_unique_tracks": len(_track_ids_seen),
        "max_concurrent_tracks": _max_concurrent_tracks,
        "current_active_tracks": len(tracks),
        "peak_threat_score": _peak_threat_score,
        "peak_threat_track": _peak_threat_track,
        "peak_threat_time_elapsed": round(_peak_threat_time - start, 1) if _peak_threat_time else 0,
        "total_threat_events": len(_threat_events),
        "total_anomalies": len(_anomaly_events),
        "total_coord_attacks": len(_coord_attack_events),
        "total_zone_breaches": len(_zone_breach_events),
        "total_tasks": len(_task_events),
        "zones_defined": len(zones),
        "assets_deployed": len(assets),
    }

    # ── Threat Analysis ──
    level_dist = Counter(e["level"] for e in _threat_events)
    intent_dist = Counter(e["intent"] for e in _threat_events)

    # Per-track peak threat
    track_peaks: Dict[str, Tuple[int, str, str]] = {}  # tid -> (score, level, intent)
    for e in _threat_events:
        tid = e["track_id"]
        if tid not in track_peaks or e["score"] > track_peaks[tid][0]:
            track_peaks[tid] = (e["score"], e["level"], e["intent"])

    # Top threatening tracks (sorted by peak score desc)
    top_threats = sorted(
        [{"track_id": tid, "peak_score": s, "peak_level": l, "peak_intent": i}
         for tid, (s, l, i) in track_peaks.items()],
        key=lambda x: x["peak_score"],
        reverse=True,
    )[:10]

    threat_analysis = {
        "level_distribution": dict(level_dist),
        "intent_distribution": dict(intent_dist),
        "top_threatening_tracks": top_threats,
        "high_threat_count": level_dist.get("HIGH", 0),
        "medium_threat_count": level_dist.get("MEDIUM", 0),
        "low_threat_count": level_dist.get("LOW", 0),
    }

    # ── Anomaly Analysis ──
    anom_by_type = Counter(a["type"] for a in _anomaly_events)
    anom_by_sev = Counter(a["severity"] for a in _anomaly_events)

    anomaly_analysis = {
        "total": len(_anomaly_events),
        "by_type": dict(anom_by_type),
        "by_severity": dict(anom_by_sev),
        "recent": _anomaly_events[-5:] if _anomaly_events else [],
    }

    # ── Coordinated Attack Analysis ──
    coord_by_subtype = Counter(c["subtype"] for c in _coord_attack_events)
    coord_analysis = {
        "total": len(_coord_attack_events),
        "by_subtype": dict(coord_by_subtype),
        "events": _coord_attack_events[-10:] if _coord_attack_events else [],
        "pincer_count": sum(1 for c in _coord_attack_events if "PINCER" in c.get("subtype", "")),
        "convergence_count": sum(1 for c in _coord_attack_events if "CONVERGE" in c.get("subtype", "")),
    }

    # ── Zone Breach Analysis ──
    breach_by_zone = Counter(b["zone_name"] for b in _zone_breach_events)
    breach_by_track = Counter(b["track_id"] for b in _zone_breach_events)
    breach_analysis = {
        "total": len(_zone_breach_events),
        "by_zone": dict(breach_by_zone),
        "by_track": dict(breach_by_track),
        "recent": _zone_breach_events[-5:] if _zone_breach_events else [],
    }

    # ── Task / Engagement Summary ──
    task_by_action = Counter(t["action"] for t in _task_events)
    task_by_status = Counter(t["status"] for t in _task_events)
    # Merge with current task statuses
    for tid, task in tasks.items():
        if task.get("status") and task["status"] != "PENDING":
            task_by_status[task["status"]] = task_by_status.get(task["status"], 0)

    task_summary = {
        "total_created": len(_task_events),
        "by_action": dict(task_by_action),
        "by_status": dict(task_by_status),
        "pending_count": sum(1 for t in tasks.values() if t.get("status") == "PENDING"),
        "approved_count": sum(1 for t in tasks.values() if t.get("status") == "APPROVED"),
        "rejected_count": sum(1 for t in tasks.values() if t.get("status") == "REJECTED"),
    }

    # ── Track Analysis (per-track summary from timelines) ──
    track_summaries = []
    if timelines:
        for tid, tl_data in timelines.items():
            if not tl_data:
                continue
            scores = [d["score"] for d in tl_data]
            intents = [d["intent"] for d in tl_data]
            levels = [d["level"] for d in tl_data]
            anomaly_count = sum(len(d.get("events", [])) for d in tl_data)
            intent_changes = sum(1 for i in range(1, len(intents)) if intents[i] != intents[i-1])

            track_summaries.append({
                "track_id": tid,
                "data_points": len(tl_data),
                "peak_score": max(scores) if scores else 0,
                "avg_score": round(sum(scores) / len(scores), 1) if scores else 0,
                "final_score": scores[-1] if scores else 0,
                "final_level": levels[-1] if levels else "LOW",
                "final_intent": intents[-1] if intents else "unknown",
                "dominant_intent": Counter(intents).most_common(1)[0][0] if intents else "unknown",
                "intent_changes": intent_changes,
                "anomaly_count": anomaly_count,
            })

    track_summaries.sort(key=lambda x: x["peak_score"], reverse=True)

    # ── Key Event Timeline ──
    key_timeline = []
    for ev in _key_events:
        elapsed = round(ev["t"] - start, 1) if start else 0
        key_timeline.append({
            "type": ev["type"],
            "message": ev["message"],
            "severity": ev["severity"],
            "elapsed_s": elapsed,
            "elapsed_display": _format_duration(elapsed),
        })

    # ── Risk Assessment ──
    risk_level = "LOW"
    risk_reasons = []
    if _peak_threat_score >= 80:
        risk_level = "CRITICAL"
        risk_reasons.append(f"Zirve tehdit skoru: {_peak_threat_score}")
    elif _peak_threat_score >= 60:
        risk_level = "HIGH"
        risk_reasons.append(f"Yuksek tehdit skoru: {_peak_threat_score}")
    elif _peak_threat_score >= 40:
        risk_level = "MEDIUM"
        risk_reasons.append(f"Orta seviye tehdit: {_peak_threat_score}")

    if len(_coord_attack_events) > 0:
        if risk_level not in ("CRITICAL",):
            risk_level = "CRITICAL"
        risk_reasons.append(f"{len(_coord_attack_events)} koordineli saldiri tespit edildi")

    pincer_count = sum(1 for c in _coord_attack_events if "PINCER" in c.get("subtype", ""))
    if pincer_count > 0:
        risk_reasons.append(f"{pincer_count} kiskac manevra tespit edildi")

    if len(_zone_breach_events) > 0:
        risk_reasons.append(f"{len(_zone_breach_events)} bolge ihlali")

    high_threat_tracks = sum(1 for tp in track_peaks.values() if tp[1] == "HIGH")
    if high_threat_tracks >= 3:
        risk_reasons.append(f"{high_threat_tracks} hedef YUKSEK tehdit seviyesine ulasti")

    risk_assessment = {
        "overall_risk": risk_level,
        "reasons": risk_reasons,
    }

    return {
        "generated_at": now,
        "generated_at_iso": _iso_now(),
        "session_active": _session["active"],
        "executive_summary": executive,
        "threat_analysis": threat_analysis,
        "anomaly_analysis": anomaly_analysis,
        "coordinated_attack_analysis": coord_analysis,
        "zone_breach_analysis": breach_analysis,
        "task_summary": task_summary,
        "track_summaries": track_summaries[:15],
        "key_event_timeline": key_timeline,
        "risk_assessment": risk_assessment,
    }


# ── Helpers ────────────────────────────────────────────────────────────────

def _format_duration(seconds: float) -> str:
    """Format seconds into Xm Ys display."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def get_status() -> Dict[str, Any]:
    """Quick status for AI status endpoint."""
    start = _session.get("start_time") or 0
    return {
        "active": _session["active"],
        "duration_s": round(time.time() - start, 1) if start else 0,
        "tracks_seen": len(_track_ids_seen),
        "threat_events": len(_threat_events),
        "anomaly_events": len(_anomaly_events),
        "coord_attacks": len(_coord_attack_events),
        "zone_breaches": len(_zone_breach_events),
        "key_events": len(_key_events),
    }


# ── Lifecycle ──────────────────────────────────────────────────────────────

def reset() -> None:
    """Reset all AAR state."""
    _session["start_time"] = None
    _session["end_time"] = None
    _session["active"] = False

    global _peak_threat_score, _peak_threat_track, _peak_threat_time
    global _max_concurrent_tracks
    _peak_threat_score = 0
    _peak_threat_track = ""
    _peak_threat_time = 0.0
    _max_concurrent_tracks = 0

    _track_ids_seen.clear()
    _threat_events.clear()
    _anomaly_events.clear()
    _coord_attack_events.clear()
    _zone_breach_events.clear()
    _task_events.clear()
    _key_events.clear()
