"""
cop/routers/ingest.py  —  POST /ingest

Extracted from cop/server.py. Contains:
  POST /ingest                — main sensor/fusion event intake
  _point_in_polygon           — ray-casting geo test
  _check_zone_breaches        — per-track zone crossing detection
  _auto_task                  — autonomous task proposal from threat events
  _rate_limit_check           — token-bucket per-IP rate limiter
"""
from __future__ import annotations

import asyncio
import logging
import os
import time as _time_mod
from typing import Any, Dict, List, Set

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from cop.state import (
    STATE, STATE_LOCK, METRICS,
    BREACH_STATE, TASK_EMITTED,
    AI_ML_PREDICTIONS,
    _TRACK_HISTORY_MAX, _track_histories,
)
from cop.ws_broadcast import broadcast, append_event_tail as _append_event_tail
from cop.helpers import utc_now_iso as _utc_now_iso, new_id as _new_id
from cop.db_writes import (
    db_write as _db_write,
    persist_track as _persist_track,
    persist_threat as _persist_threat,
    persist_alert as _persist_alert,
    persist_task as _persist_task,
)
from cop.state import make_snapshot_payload as _make_snapshot_payload

from ai import aar as ai_aar
from ai import lineage as ai_lineage
from ai import timeline as ai_timeline
from ai import trajectory as ai_trajectory
from ai import track_fsm
from ai import deconfliction as ai_deconfliction
from ai import ew_detector as ai_ew
from ai import ew_ml as ai_ew_ml
from ai import fusion as ai_fusion
from ai.fusion import SensorMeasurement as FusionMeasurement
from ai import nonlethal as ai_nonlethal
from cop.engine.ai_pipeline import (
    process_track as _ai_process_track,
    schedule_ai_tactical as _schedule_ai_tactical,
)
from cop import circuit_breaker as cop_cb
from cop import webhooks as cop_webhooks
from replay import recorder as replay_recorder

try:
    from auth.deps import AUTH_ENABLED
except ImportError:
    AUTH_ENABLED = False  # type: ignore

log = logging.getLogger("nizam.cop")
router = APIRouter()

INGEST_API_KEY = os.environ.get("INGEST_API_KEY", "")

# Boot-time guard: refuse to start if auth is on but the ingest key is unset.
# Otherwise the line "if AUTH_ENABLED and INGEST_API_KEY" silently fails open
# when the key is empty — every unauthenticated request is accepted, which is
# the exact opposite of what an operator who flipped AUTH_ENABLED expects.
# Same fail-closed pattern as the JWT_SECRET guard in auth/deps.py.
if AUTH_ENABLED and not INGEST_API_KEY:
    raise RuntimeError(
        "AUTH_ENABLED=true but INGEST_API_KEY is not set. /ingest would "
        "fail-open (accept all requests without authentication). Refusing "
        "to boot. Set INGEST_API_KEY to a secure value, e.g.: "
        'python -c "import secrets; print(secrets.token_urlsafe(32))"'
    )

# ── Rate limiter (token bucket per IP) ───────────────────────────────────────
_RATE_LIMIT_RPS   = 200   # max requests per second per IP
_RATE_LIMIT_BURST = 500   # burst capacity
_rate_buckets: Dict[str, list] = {}  # {ip: [tokens, last_refill_time]}


def _rate_limit_check(ip: str) -> bool:
    """Return True if the request is allowed, False if rate-limited."""
    now = _time_mod.monotonic()
    bucket = _rate_buckets.get(ip)
    if bucket is None:
        _rate_buckets[ip] = [_RATE_LIMIT_BURST - 1, now]
        return True
    tokens, last = bucket
    elapsed = now - last
    tokens = min(_RATE_LIMIT_BURST, tokens + elapsed * _RATE_LIMIT_RPS)
    if tokens < 1.0:
        bucket[0] = tokens
        bucket[1] = now
        return False
    bucket[0] = tokens - 1
    bucket[1] = now
    return True


# ── Geo helper ────────────────────────────────────────────────────────────────

def _point_in_polygon(lat: float, lon: float, coords: List) -> bool:
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


# ── Zone breach detection ─────────────────────────────────────────────────────

async def _check_zone_breaches(track_id: str, lat: float, lon: float) -> None:
    current_breaches: Set[str] = set()
    for zone_id, zone in STATE["zones"].items():
        coords = zone.get("coordinates", [])
        if _point_in_polygon(lat, lon, coords):
            current_breaches.add(zone_id)

    prev_breaches = BREACH_STATE.get(track_id, set())
    new_entries   = current_breaches - prev_breaches
    BREACH_STATE[track_id] = current_breaches

    for zone_id in new_entries:
        zone = STATE["zones"].get(zone_id, {})
        alert_payload = {
            "alert_type": "zone_breach",
            "track_id":   track_id,
            "zone_id":    zone_id,
            "zone_name":  zone.get("name", zone_id),
            "zone_type":  zone.get("type", "restricted"),
            "lat": lat, "lon": lon,
            "server_time": _utc_now_iso(),
        }
        alert = {"event_type": "cop.alert", "payload": alert_payload}
        _append_event_tail(alert)
        await broadcast(alert)
        asyncio.create_task(_db_write(_persist_alert(alert_payload)))
        ai_aar.record_zone_breach(alert_payload)
        asyncio.create_task(cop_webhooks.dispatch("cop.zone_breach", alert_payload))


# ── Autonomous tasking ────────────────────────────────────────────────────────

_ACTION_MAP = {
    "attack":         ("ENGAGE",  "HIGH"),
    "reconnaissance": ("OBSERVE", "MEDIUM"),
    "loitering":      ("OBSERVE", "MEDIUM"),
    "unknown":        ("OBSERVE", "HIGH"),
}


async def _auto_task(threat_id: str, threat_payload: Dict[str, Any]) -> None:
    level  = threat_payload.get("threat_level", "LOW")
    intent = threat_payload.get("intent", "unknown")

    if level not in ("HIGH", "MEDIUM"):
        return

    emitted = TASK_EMITTED.get(threat_id, set())
    action, _ = _ACTION_MAP.get(intent, ("OBSERVE", "HIGH"))

    if action == "ENGAGE" and level != "HIGH":
        action = "OBSERVE"

    task_key = f"{action}:{intent}"
    if task_key not in emitted:
        already = any(
            t["track_id"] == threat_id and t["action"] == action and t["status"] == "PENDING"
            for t in STATE["tasks"].values()
        )
        if not already:
            task = {
                "id":           _new_id("task-"),
                "track_id":     threat_id,
                "action":       action,
                "threat_level": level,
                "intent":       intent,
                "score":        threat_payload.get("score", 0),
                "tti_s":        threat_payload.get("tti_s"),
                "status":       "PENDING",
                "created_at":   _utc_now_iso(),
                "resolved_at":  None,
                "resolved_by":  None,
            }
            STATE["tasks"][task["id"]] = task
            TASK_EMITTED.setdefault(threat_id, set()).add(task_key)
            ev = {"event_type": "cop.task", "payload": task}
            _append_event_tail(ev)
            await broadcast(ev)
            asyncio.create_task(_db_write(_persist_task(task)))
            ai_aar.record_task(task)
            if level == "HIGH":
                asyncio.create_task(cop_webhooks.dispatch("cop.threat_high", {
                    "track_id": threat_id, "action": action,
                    "threat_level": level, "intent": intent,
                    "score": task["score"],
                }))
            try:
                ai_lineage.record(
                    track_id=threat_id,
                    stage="task_proposer",
                    summary=f"Auto-proposed {action} (intent={intent}, level={level})",
                    inputs={
                        "threat_level": level,
                        "intent": intent,
                        "score": threat_payload.get("score", 0),
                        "tti_s": threat_payload.get("tti_s"),
                    },
                    outputs={"task_id": task["id"], "action": action, "status": "PENDING"},
                    rule=f"auto_task.{intent}→{action}",
                )
            except Exception:
                pass

    # ── Non-lethal alternatives (alongside ENGAGE for score < 90) ────────────
    if action == "ENGAGE":
        nl_options = ai_nonlethal.recommend(
            threat_id, threat_payload, dict(STATE["assets"])
        )
        for opt in nl_options:
            nl_action = opt["action"]
            nl_key    = f"{nl_action}:{intent}"
            if nl_key in TASK_EMITTED.get(threat_id, set()):
                continue
            already_nl = any(
                t["track_id"] == threat_id and t["action"] == nl_action and t["status"] == "PENDING"
                for t in STATE["tasks"].values()
            )
            if already_nl:
                continue
            nl_task = {
                "id":            _new_id("task-"),
                "track_id":      threat_id,
                "action":        nl_action,
                "threat_level":  level,
                "intent":        intent,
                "score":         threat_payload.get("score", 0),
                "tti_s":         threat_payload.get("tti_s"),
                "effector_id":   opt.get("effector_id"),
                "effector_name": opt.get("effector_name"),
                "dist_km":       opt.get("dist_km"),
                "status":        "PENDING",
                "created_at":    _utc_now_iso(),
                "resolved_at":   None,
                "resolved_by":   None,
            }
            STATE["tasks"][nl_task["id"]] = nl_task
            TASK_EMITTED.setdefault(threat_id, set()).add(nl_key)
            nl_ev = {"event_type": "cop.task", "payload": nl_task}
            _append_event_tail(nl_ev)
            await broadcast(nl_ev)
            asyncio.create_task(_db_write(_persist_task(nl_task)))


# ── Main ingest endpoint ──────────────────────────────────────────────────────

_VALID_EVENT_TYPES = {"cop.track", "cop.threat", "cop.zone", "cop.alert",
                      "cop.asset", "cop.task", "cop.waypoint"}


@router.post("/ingest")
async def ingest(req: Request):
    client_ip = req.client.host if req.client else "unknown"

    # Circuit breaker — reject before any work if IP is in OPEN/HALF_OPEN probe
    cb_ok, cb_reason = cop_cb.check(client_ip)
    if not cb_ok:
        METRICS["ingest_bad_request"] += 1
        return JSONResponse({"ok": False, "error": cb_reason}, status_code=503,
                            headers={"Retry-After": "30"})

    # API key guard: when AUTH_ENABLED, require X-API-Key header.
    # Two layers of defence:
    #   1. Boot guard refuses startup with empty INGEST_API_KEY.
    #   2. Runtime check below explicitly rejects when the key is empty
    #      so an empty header against an empty key cannot match (""=="").
    if AUTH_ENABLED:
        if not INGEST_API_KEY:
            METRICS["ingest_bad_request"] += 1
            cop_cb.record_bad(client_ip)
            return JSONResponse(
                {"ok": False, "error": "ingest API key not configured"},
                status_code=503,
            )
        provided = req.headers.get("x-api-key", "")
        if provided != INGEST_API_KEY:
            METRICS["ingest_bad_request"] += 1
            cop_cb.record_bad(client_ip)
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    # Rate limiting
    if not _rate_limit_check(client_ip):
        METRICS["ingest_bad_request"] += 1
        cop_cb.record_bad(client_ip)
        return JSONResponse({"ok": False, "error": "rate limited"}, status_code=429)

    # Size guard — reject payloads > 256 KB
    content_length = req.headers.get("content-length")
    if content_length and int(content_length) > 262_144:
        METRICS["ingest_bad_request"] += 1
        cop_cb.record_bad(client_ip)
        return JSONResponse({"ok": False, "error": "payload too large"}, status_code=413)

    try:
        body = await req.json()
    except Exception:
        METRICS["ingest_bad_request"] += 1
        cop_cb.record_bad(client_ip)
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    # Pydantic shape validation — rejects malformed envelopes cleanly instead
    # of letting them detonate inside the fusion engine.
    from pydantic import ValidationError
    from schemas.models import EventEnvelope
    try:
        envelope = EventEnvelope.model_validate(body)
    except ValidationError as exc:
        METRICS["ingest_bad_request"] += 1
        cop_cb.record_bad(client_ip)
        return JSONResponse(
            {"ok": False, "error": "schema validation failed", "detail": exc.errors()[:3]},
            status_code=400,
        )
    event_type = envelope.event_type
    payload    = envelope.payload

    # Validate event_type whitelist
    if event_type not in _VALID_EVENT_TYPES:
        METRICS["ingest_bad_request"] += 1
        cop_cb.record_bad(client_ip)
        return JSONResponse({"ok": False, "error": f"unknown event_type: {event_type}"}, status_code=400)

    METRICS["ingest_total"] += 1
    METRICS["ingest_by_type"][event_type] = METRICS["ingest_by_type"].get(event_type, 0) + 1
    cop_cb.record_success(client_ip)

    if isinstance(payload, dict) and "server_time" not in payload:
        payload["server_time"] = _utc_now_iso()

    ev = {"event_type": event_type, "payload": payload}

    async with STATE_LOCK:
        _append_event_tail(ev)

        if event_type == "cop.track":
            track_id = (
                payload.get("id") or payload.get("track_id")
                or payload.get("global_track_id") or payload.get("gid")
            )
            if track_id is not None:
                raw_id = str(track_id)

                # ── Deconfliction: resolve alias or detect duplicate ──────────
                canonical_id = raw_id
                match = ai_deconfliction.find_match(payload, STATE["tracks"])
                if match is not None:
                    matched_id, score = match
                    if matched_id != raw_id:
                        existing = STATE["tracks"].get(matched_id, {})
                        payload["supporting_sensors"] = ai_deconfliction.merge_sensors(
                            existing, payload
                        )
                        payload["id"] = matched_id
                        payload["_deconfliction"] = {
                            "alias": raw_id,
                            "canonical": matched_id,
                            "score": score,
                        }
                        ai_deconfliction.record_merge(raw_id, matched_id)
                        ai_aar.record_deconfliction_merge(raw_id, matched_id)
                        canonical_id = matched_id
                        asyncio.create_task(broadcast({
                            "event_type": "cop.track_merged",
                            "payload": {
                                "alias_id":     raw_id,
                                "canonical_id": matched_id,
                                "score":        score,
                                "server_time":  _utc_now_iso(),
                            },
                        }))

                track_id = canonical_id

                # ── Multi-sensor fusion ───────────────────────────────────────
                sensors = payload.get("supporting_sensors", [])
                lat_raw = payload.get("lat")
                lon_raw = payload.get("lon")
                if lat_raw is not None and lon_raw is not None:
                    _sensor_id = sensors[0] if sensors else "unknown"
                    _meas = FusionMeasurement(
                        sensor_id   = _sensor_id,
                        track_hint  = str(track_id),
                        lat         = float(lat_raw),
                        lon         = float(lon_raw),
                        alt_m       = float((payload.get("kinematics") or {}).get("altitude_m") or
                                            payload.get("altitude_m") or 0.0),
                        speed_mps   = float((payload.get("kinematics") or {}).get("speed_mps") or
                                            payload.get("speed_mps") or 0.0),
                        heading_deg = float((payload.get("kinematics") or {}).get("heading_deg") or
                                            payload.get("heading_deg") or 0.0),
                        timestamp   = payload.get("server_time", _utc_now_iso()),
                    )
                    _fused = ai_fusion.engine.update(_meas)
                    payload["lat"] = _fused.lat
                    payload["lon"] = _fused.lon
                    payload["_fusion"] = {
                        "fused_id":             _fused.id,
                        "contributing_sensors": _fused.contributing_sensors,
                        "pos_std_m":            round(_fused.pos_std_m, 1),
                        "speed_std_mps":        round(_fused.speed_std_mps, 2),
                        "sensor_count":         len(_fused.contributing_sensors),
                    }
                    if payload.get("kinematics"):
                        payload["kinematics"]["altitude_m"]  = _fused.alt_m
                        payload["kinematics"]["speed_mps"]   = _fused.speed_mps
                        payload["kinematics"]["heading_deg"] = _fused.heading_deg

                # Track FSM: update lifecycle state
                fsm_state = track_fsm.on_update(str(track_id), sensors)
                payload["track_state"] = fsm_state.value

                STATE["tracks"][str(track_id)] = payload
                lat = payload.get("lat")
                lon = payload.get("lon")

                # ── Rolling breadcrumb trail ──────────────────────────────────
                if lat is not None and lon is not None:
                    _tid = str(track_id)
                    hist = _track_histories.setdefault(_tid, [])
                    hist.append({"lat": round(float(lat), 6), "lon": round(float(lon), 6)})
                    if len(hist) > _TRACK_HISTORY_MAX:
                        del hist[:len(hist) - _TRACK_HISTORY_MAX]
                    payload["history"] = list(hist)
                if lat is not None and lon is not None and STATE["zones"]:
                    await _check_zone_breaches(str(track_id), float(lat), float(lon))

                # ── EW detection: rule-based + statistical ML classifiers ─────
                if lat is not None and lon is not None:
                    speed_ms = float(payload.get("speed_ms") or payload.get("speed") or 0.0)
                    heading  = float(payload.get("heading_deg") or payload.get("heading") or 0.0)
                    ew_alerts = ai_ew.on_track_update(
                        str(track_id), float(lat), float(lon),
                        sensors=sensors,
                    )
                    ew_alerts += ai_ew_ml.on_track_update(
                        str(track_id), float(lat), float(lon),
                        speed_ms=speed_ms, heading=heading,
                    )
                    for alert in ew_alerts:
                        ai_aar.record_ew_alert(alert)
                        _ew_payload = {**alert, "server_time": _utc_now_iso()}
                        asyncio.create_task(broadcast({
                            "event_type": "cop.ew_alert",
                            "payload":    _ew_payload,
                        }))
                        asyncio.create_task(cop_webhooks.dispatch("cop.ew_alert", _ew_payload))

                # ── Phase 5: AI hooks on track update ─────────────────────────
                if lat is not None and lon is not None:
                    _ai_process_track(str(track_id), float(lat), float(lon),
                                      payload.get("intent", "unknown"))
                # AAR: record track
                ai_aar.record_track(str(track_id), len(STATE["tracks"]))

                # Decision lineage: first sighting of this track.
                try:
                    ai_lineage.record(
                        track_id=str(track_id),
                        stage="ingest",
                        summary=f"Track update — sensors: {', '.join(sensors) if sensors else 'fuser'}",
                        inputs={
                            "lat": lat, "lon": lon,
                            "speed": payload.get("speed"),
                            "heading": payload.get("heading"),
                            "classification": payload.get("classification"),
                            "sensors": sensors,
                        },
                        outputs={"state": fsm_state.value},
                        rule="cop.ingest",
                    )
                except Exception:
                    pass

            asyncio.create_task(_db_write(_persist_track(payload)))

        elif event_type == "cop.threat":
            threat_id = (
                payload.get("id") or payload.get("track_id")
                or payload.get("global_track_id") or payload.get("gid")
            )
            if threat_id is not None:
                STATE["threats"][str(threat_id)] = payload
                await _auto_task(str(threat_id), payload)
                ai_timeline.record_threat(
                    str(threat_id),
                    score=int(payload.get("score", payload.get("threat_score", 0))),
                    level=str(payload.get("threat_level", "LOW")),
                    intent=str(payload.get("intent", "unknown")),
                )
                ai_aar.record_threat(
                    str(threat_id),
                    score=int(payload.get("score", payload.get("threat_score", 0))),
                    level=str(payload.get("threat_level", "LOW")),
                    intent=str(payload.get("intent", "unknown")),
                )
                try:
                    t_level  = str(payload.get("threat_level", "LOW"))
                    t_score  = payload.get("score", payload.get("threat_score", 0))
                    t_intent = str(payload.get("intent", "unknown"))
                    ai_lineage.record(
                        track_id=str(threat_id),
                        stage="threat_assess",
                        summary=f"Threat → {t_level} (score={t_score}, intent={t_intent})",
                        inputs={
                            "threat_level": t_level,
                            "score": t_score,
                            "intent": t_intent,
                            "tti_s": payload.get("tti_s"),
                            "classification": payload.get("classification"),
                        },
                        outputs={"threat_level": t_level, "score": t_score},
                        rule="cop.threat_ingest",
                    )
                except Exception:
                    pass
            asyncio.create_task(_db_write(_persist_threat(payload)))

        elif event_type == "cop.snapshot":
            pass

    # ── Phase 5: fire-and-forget tactical analysis ────────────────────────────
    _schedule_ai_tactical()

    # ── Record frame for replay ───────────────────────────────────────────────
    replay_recorder.capture_frame(_make_snapshot_payload)

    await broadcast(ev)
    return JSONResponse({"ok": True})
