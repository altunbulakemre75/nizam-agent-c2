"""
cop/engine/ai_pipeline.py  —  AI analysis pipeline (tick orchestrator)

Runs the per-tick AI pipeline that fans out to every analyzer module:
  ai.predictor, ai.anomaly, ai.tactical (recommendation engine),
  ai.coordinated_attack, ai.ml_threat, ai.roe, ai.zone_breach, ...

NOTE on naming: this module is the *pipeline*, not a single analyzer.
One of the analyzers it runs is `ai/tactical.py` (the recommendation
engine). They used to share the name `tactical.py` which was a
perpetual foot-gun — "which tactical am I importing?". Renamed to
ai_pipeline to make the distinction obvious:
    ai.tactical         → one analyzer (recommendations)
    cop.engine.ai_pipeline → orchestrator that runs all analyzers

Public API:
    from cop.engine.ai_pipeline import schedule_ai_tactical, process_track

Architecture
------------
  process_track()               — per-track Kalman + LSTM + anomaly (from /ingest)
  _ai_run_tactical_compute      — pure-compute function (runs in thread pool)
  _ai_tactical_background_task  — async wrapper: snapshot → executor → apply results
  schedule_ai_tactical()        — rate-limited scheduler called from /ingest
"""
from __future__ import annotations

import asyncio
import logging
import time as _time_mod
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict

# ── AI Decision Support imports ──────────────────────────────────────────────
from ai import predictor as ai_predictor
from ai import anomaly as ai_anomaly
from ai import tactical as ai_tactical
from ai import zone_breach as ai_zone_breach
from ai import coordinated_attack as ai_coord_attack
from ai import timeline as ai_timeline
from ai import aar as ai_aar
from ai import roe as ai_roe
from ai import ml_threat as ai_ml
from ai import confidence as ai_confidence
from ai import trajectory as ai_trajectory
from ai import ew_detector as ai_ew
from ai import ew_ml as ai_ew_ml
from ai import escalation as ai_escalation
from ai import assignment as ai_assignment
from ai import blue_force as ai_blue_force
from ai import drift as ai_drift
from ai import registry as ai_registry
from ai.registry import TacticalContext as _TacticalContext

# ── Cop imports ───────────────────────────────────────────────────────────────
from cop.state import (
    STATE, STATE_LOCK, METRICS,
    AI_PREDICTIONS, AI_TRAJECTORIES, AI_ANOMALIES,
    AI_RECOMMENDATIONS, AI_PRED_BREACHES, AI_UNCERTAINTY_CONES,
    AI_COORD_ATTACKS, AI_ROE_ADVISORIES, AI_ASSIGNMENT, AI_BFT_WARNINGS,
    EFFECTOR_OUTCOMES, AI_DRIFT_STATUS, AI_ML_PREDICTIONS, AI_ML_PREV_TRACKS,
    AI_PLUGIN_RESULTS, AI_ANOMALY_MAX,
    metrics_record_tactical_duration,
)
from cop.ws_broadcast import broadcast
from cop.helpers import utc_now_iso as _utc_now_iso
from cop.otel import span as otel_span

log = logging.getLogger("nizam.cop")

# ── Engine state ──────────────────────────────────────────────────────────────

_ai_tactical_last = 0.0
_AI_TACTICAL_INTERVAL = 1.0   # run tactical engine every N seconds (was 3.0)
_ai_tactical_bg_lock = asyncio.Lock()


# ── Per-track fast path (called from /ingest) ─────────────────────────────────

def process_track(track_id: str, lat: float, lon: float, intent: str) -> None:
    """Run Kalman predictor + LSTM trajectory + anomaly detection on a single track update.

    Called synchronously from the /ingest handler for every cop.track event so
    predictions and anomalies are always up-to-date before the tactical pass.
    """
    track = STATE["tracks"].get(track_id, {})
    speed   = float(track.get("speed") or track.get("kinematics", {}).get("speed_mps") or 0.0)
    heading = float(track.get("heading") or track.get("kinematics", {}).get("heading_deg") or 0.0)

    # 1) Kalman filter prediction
    preds = ai_predictor.update_track(track_id, lat, lon)
    if preds:
        AI_PREDICTIONS[track_id] = preds

    # 2) LSTM trajectory prediction
    ai_trajectory.update(track_id, lat, lon, speed=speed, heading=heading)
    traj = ai_trajectory.predict(track_id)
    if traj:
        AI_TRAJECTORIES[track_id] = traj

    # 3) Anomaly detection
    anomalies = ai_anomaly.check_track(track_id, lat, lon, intent=intent)
    if anomalies:
        AI_ANOMALIES.extend(anomalies)
        if len(AI_ANOMALIES) > AI_ANOMALY_MAX:
            del AI_ANOMALIES[: len(AI_ANOMALIES) - AI_ANOMALY_MAX]
        for a in anomalies:
            ai_timeline.record_anomaly(
                track_id, a.get("type", "UNKNOWN"), a.get("severity", "MEDIUM"),
            )
            ai_aar.record_anomaly(a)


# ── Pure-compute tactical pass (runs in thread pool executor) ─────────────────

def _ai_run_tactical_compute(
    tracks_snap: Dict[str, Dict],
    threats_snap: Dict[str, Dict],
    assets_snap: Dict[str, Dict],
    zones_snap: Dict[str, Dict],
) -> Dict[str, Any]:
    """
    Pure-compute tactical engine pass. Runs in a thread pool executor so
    it does NOT block the asyncio event loop during heavy ML / analysis.

    Sub-modules are executed in PARALLEL via concurrent.futures.ThreadPoolExecutor.
    numpy-backed modules (anomaly, coordinated_attack) release GIL during
    heavy compute, enabling true CPU parallelism across threads.

    Dependency graph:
      Group A (all independent — run in parallel):
        swarm, tactical, zone_breach, cones, coord_attack, ml_threat, ew
      Group B (depends on coord_attack result):
        confidence, roe
      Group C (additive plugins):
        ai_registry.run_all()

    Returns a dict of results. The caller is responsible for applying
    them to the AI_* globals on the event loop thread.
    """
    _timings: Dict[str, float] = {}

    # ── Inject context for plugin analyzers ─────────────────────────
    ai_registry.set_context(_TacticalContext(
        tracks=tracks_snap, threats=threats_snap,
        assets=assets_snap, zones=zones_snap,
    ))

    # ── Timed wrapper ────────────────────────────────────────────────
    def _timed(name, fn, *a, **kw):
        t0 = _time_mod.monotonic()
        r = fn(*a, **kw)
        _timings[name] = round((_time_mod.monotonic() - t0) * 1000, 2)
        return r

    # ── Group A: independent sub-modules — run in parallel ──────────
    _results: Dict[str, Any] = {}

    def _run_swarm():
        return _timed("swarm", ai_anomaly.detect_swarms, tracks_snap)

    def _run_tactical():
        return _timed("tactical", ai_tactical.generate_recommendations,
                       tracks=tracks_snap, threats=threats_snap,
                       assets=assets_snap, zones=zones_snap,
                       anomalies=AI_ANOMALIES, predictions=AI_PREDICTIONS)

    def _run_zone_breach():
        return _timed("zone_breach", ai_zone_breach.check_predictive_breaches,
                       predictions=AI_PREDICTIONS, zones=zones_snap)

    def _run_cones():
        return _timed("cones", ai_zone_breach.build_uncertainty_cones, AI_PREDICTIONS)

    def _run_coord_attack():
        return _timed("coord_attack", ai_coord_attack.detect_coordinated_attacks,
                       tracks=tracks_snap, predictions=AI_PREDICTIONS,
                       zones=zones_snap, assets=assets_snap)

    def _run_ml():
        if not ai_ml.is_available():
            _timings["ml_threat"] = 0.0
            return {}
        return _timed("ml_threat", ai_ml.predict_batch,
                       tracks=tracks_snap, threats=threats_snap,
                       assets=assets_snap, zones=zones_snap,
                       prev_tracks=AI_ML_PREV_TRACKS, dt=_AI_TACTICAL_INTERVAL)

    def _run_ew():
        alerts = _timed("ew", ai_ew.check_mass_jamming, tracks_snap)
        alerts += ai_ew_ml.check_patterns(tracks_snap)
        return alerts

    # 7 independent tasks — ThreadPoolExecutor with numpy GIL-release
    with ThreadPoolExecutor(max_workers=7, thread_name_prefix="tac") as pool:
        futures = {
            pool.submit(_run_swarm):        "swarm_anomalies",
            pool.submit(_run_tactical):     "recommendations",
            pool.submit(_run_zone_breach):  "pred_breaches",
            pool.submit(_run_cones):        "uncertainty_cones",
            pool.submit(_run_coord_attack): "coord_attacks",
            pool.submit(_run_ml):           "ml_predictions",
            pool.submit(_run_ew):           "ew_alerts",
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                _results[key] = future.result()
            except Exception as exc:
                log.warning("[cop] tactical sub-module %s failed: %s", key, exc)
                _results[key] = [] if key != "ml_predictions" and key != "uncertainty_cones" else {}

    # ── Group B: Confidence scoring, then ROE (depends on confidence) ──
    coord_attacks  = _results.get("coord_attacks", [])
    ml_predictions = _results.get("ml_predictions", {})
    ew_alerts_flat = list(_results.get("ew_alerts", []))

    with otel_span("tactical.confidence", {"tracks": len(tracks_snap), "threats": len(threats_snap)}):
        enriched_threats = _timed(
            "confidence",
            ai_confidence.score_batch,
            tracks=tracks_snap,
            threats=threats_snap,
            ml_predictions=ml_predictions,
            ew_alerts=ew_alerts_flat,
        )

    with otel_span("tactical.roe", {"advisories_in": len(enriched_threats)}):
        roe_advs = _timed("roe", ai_roe.evaluate_all,
                           tracks=tracks_snap, threats=enriched_threats,
                           zones=zones_snap, assets=assets_snap,
                           coord_attacks=coord_attacks)

    # ── Group C: Plugin analyzers (registry) — run after core modules ──
    # Any module that called ai_registry.register() (without editing
    # server.py) gets executed here in declaration order. The current
    # TacticalContext snapshot is available via ai_registry.get_context().
    plugin_results = ai_registry.run_all(stage="tactical.analyze")
    if plugin_results:
        _timings["plugins"] = round(
            sum(v.get("elapsed_ms", 0) for v in plugin_results.values()), 2
        )

    return {
        "swarm_anomalies":   list(_results.get("swarm_anomalies", [])),
        "recommendations":   list(_results.get("recommendations", [])),
        "pred_breaches":     list(_results.get("pred_breaches", [])),
        "uncertainty_cones": dict(_results.get("uncertainty_cones", {})),
        "coord_attacks":     list(coord_attacks),
        "ml_predictions":    ml_predictions,
        "enriched_threats":  enriched_threats,
        "roe_advisories":    list(roe_advs),
        "ew_alerts":         ew_alerts_flat,
        "plugin_results":    plugin_results,
        "_timings_ms":       _timings,
    }


# ── Async background task ─────────────────────────────────────────────────────

async def _ai_tactical_background_task() -> None:
    """
    Background task: snapshot state, run the tactical engine off-loop in
    an executor, then apply results + broadcast.

    Guarded by ``_ai_tactical_bg_lock`` so at most one pass is in flight.
    If a previous pass is still computing (e.g. ML is slow), this call
    drops the tick entirely — newer state will drive the next tick.
    """
    if _ai_tactical_bg_lock.locked():
        METRICS["tactical_overlap_skipped"] += 1
        return
    async with _ai_tactical_bg_lock:
        # 1) Snapshot state under STATE_LOCK (brief hold, shallow copies).
        async with STATE_LOCK:
            tracks_snap  = dict(STATE["tracks"])
            threats_snap = dict(STATE["threats"])
            assets_snap  = dict(STATE["assets"])
            zones_snap   = dict(STATE["zones"])

        # 2) Run the heavy compute in a thread pool executor so the event
        #    loop stays free to handle /ingest and WebSocket traffic.
        loop = asyncio.get_running_loop()
        t_start = _time_mod.perf_counter()
        try:
            result = await loop.run_in_executor(
                None,
                _ai_run_tactical_compute,
                tracks_snap, threats_snap, assets_snap, zones_snap,
            )
        except Exception as exc:
            METRICS["tactical_failed"] += 1
            log.warning("[cop] tactical compute failed: %s", exc)
            return
        finally:
            metrics_record_tactical_duration(
                (_time_mod.perf_counter() - t_start) * 1000.0
            )
        METRICS["tactical_ran"] += 1
        if "_timings_ms" in result:
            METRICS["tactical_module_ms"] = result.pop("_timings_ms")

        # 3) Apply results to AI_* globals. We're back on the event loop
        #    thread here, so writes are serialized w.r.t. /ingest handlers.
        if result["swarm_anomalies"]:
            AI_ANOMALIES.extend(result["swarm_anomalies"])
            if len(AI_ANOMALIES) > AI_ANOMALY_MAX:
                del AI_ANOMALIES[: len(AI_ANOMALIES) - AI_ANOMALY_MAX]

        AI_RECOMMENDATIONS.clear()
        AI_RECOMMENDATIONS.extend(result["recommendations"])

        AI_PRED_BREACHES.clear()
        AI_PRED_BREACHES.extend(result["pred_breaches"])

        AI_UNCERTAINTY_CONES.clear()
        AI_UNCERTAINTY_CONES.update(result["uncertainty_cones"])

        AI_COORD_ATTACKS.clear()
        AI_COORD_ATTACKS.extend(result["coord_attacks"])
        for ca in result["coord_attacks"]:
            ai_aar.record_coord_attack(ca)

        if ai_ml.is_available():
            AI_ML_PREDICTIONS.clear()
            AI_ML_PREDICTIONS.update(result["ml_predictions"])
            AI_ML_PREV_TRACKS.clear()
            AI_ML_PREV_TRACKS.update({k: dict(v) for k, v in tracks_snap.items()})

        if result.get("plugin_results"):
            AI_PLUGIN_RESULTS.clear()
            AI_PLUGIN_RESULTS.update(result["plugin_results"])

        # Stamp confidence onto live STATE["threats"] so /api/threats and
        # WebSocket clients see the up-to-date score without a full re-ingest.
        async with STATE_LOCK:
            for tid, enriched in result.get("enriched_threats", {}).items():
                if tid in STATE["threats"]:
                    STATE["threats"][tid]["confidence"]           = enriched["confidence"]
                    STATE["threats"][tid]["confidence_grade"]     = enriched["confidence_grade"]
                    STATE["threats"][tid]["confidence_breakdown"] = enriched["confidence_breakdown"]

        # ── Model drift detection ─────────────────────────────────────────────
        ai_drift.record_batch(
            enriched_threats=result.get("enriched_threats", {}),
            ml_predictions=result.get("ml_predictions", {}),
        )
        AI_DRIFT_STATUS.clear()
        AI_DRIFT_STATUS.update(ai_drift.status())
        if AI_DRIFT_STATUS.get("drift_level") == "major":
            log.warning("[drift] Major model drift detected — PSI=%.3f", AI_DRIFT_STATUS["psi"])

        # ── Blue Force / Fratricide check ─────────────────────────────────
        # Must run against current STATE so it sees live friendly positions.
        bft_screened, bft_warnings = ai_blue_force.check_advisories(
            advisories=result["roe_advisories"],
            tracks=dict(STATE["tracks"]),
            assets=dict(STATE["assets"]),
        )
        for warn in bft_warnings:
            log.warning("[bft] %s", warn["message"])
            await broadcast({
                "event_type": "cop.bft_warning",
                "payload":    {**warn, "server_time": _utc_now_iso()},
            })

        AI_ROE_ADVISORIES.clear()
        AI_ROE_ADVISORIES.extend(bft_screened)
        for adv in AI_ROE_ADVISORIES:
            ai_aar.record_roe_advisory(adv)

        # ── Multi-effector assignment ──────────────────────────────────────
        with otel_span("tactical.assignment", {
            "threats": len(STATE["threats"]), "assets": len(STATE["assets"]),
        }):
            assign_result = ai_assignment.compute(
                threats=dict(STATE["threats"]),
                assets=dict(STATE["assets"]),
                roe_advisories=AI_ROE_ADVISORIES,
            )
        AI_ASSIGNMENT["assignments"] = [
            {"threat_id": a.threat_id, "effector_id": a.effector_id,
             "effector_name": a.effector_name, "cost": a.cost,
             "dist_km": a.dist_km, "threat_score": a.threat_score,
             "engagement": a.engagement}
            for a in assign_result.assignments
        ]
        AI_ASSIGNMENT["unassigned_threats"]   = assign_result.unassigned_threats
        AI_ASSIGNMENT["unassigned_effectors"] = assign_result.unassigned_effectors
        AI_ASSIGNMENT["stats"]                = assign_result.stats

        AI_BFT_WARNINGS.clear()
        AI_BFT_WARNINGS.extend(bft_warnings)

        # ── Escalation check — unanswered advisory alarm ──────────────────
        escalations = ai_escalation.check(AI_ROE_ADVISORIES)
        for esc in escalations:
            await broadcast({
                "event_type": "cop.escalation",
                "payload":    {**esc, "server_time": _utc_now_iso()},
            })

        # Broadcast EW jamming alerts individually
        for ew_alert in result.get("ew_alerts", []):
            ai_aar.record_ew_alert(ew_alert)
            await broadcast({
                "event_type": "cop.ew_alert",
                "payload":    {**ew_alert, "server_time": _utc_now_iso()},
            })

        # 4) Broadcast AI update to connected UI clients.
        await broadcast({
            "event_type": "cop.ai_update",
            "payload": {
                "predictions":       AI_PREDICTIONS,
                "anomalies":         AI_ANOMALIES[-20:],
                "recommendations":   AI_RECOMMENDATIONS,
                "pred_breaches":     AI_PRED_BREACHES,
                "uncertainty_cones": AI_UNCERTAINTY_CONES,
                "coord_attacks":     AI_COORD_ATTACKS,
                "roe_advisories":    AI_ROE_ADVISORIES,
                "ml_predictions":    AI_ML_PREDICTIONS,
                "ml_available":      ai_ml.is_available(),
                "confidence_scores": {
                    tid: {
                        "confidence": t.get("confidence"),
                        "grade":      t.get("confidence_grade"),
                    }
                    for tid, t in STATE["threats"].items()
                    if t.get("confidence") is not None
                },
                "assignment":        dict(AI_ASSIGNMENT),
                "bft_warnings":      list(AI_BFT_WARNINGS),
                "effector_status":   dict(STATE["effector_status"]),
                "effector_outcomes": list(EFFECTOR_OUTCOMES[-10:]),
                "drift":             dict(AI_DRIFT_STATUS),
                "server_time":       _utc_now_iso(),
            },
        })


# ── Rate-limited scheduler (called from /ingest) ─────────────────────────────

def schedule_ai_tactical() -> bool:
    """
    Rate-limited scheduler for the tactical background task. Called from
    /ingest. Never blocks: at most it spawns an asyncio task and returns.

    Returns True if a task was scheduled this call, False if skipped due
    to rate limit.
    """
    global _ai_tactical_last
    METRICS["tactical_scheduled"] += 1
    now = _time_mod.time()
    if now - _ai_tactical_last < _AI_TACTICAL_INTERVAL:
        METRICS["tactical_rate_skipped"] += 1
        return False
    _ai_tactical_last = now
    asyncio.create_task(_ai_tactical_background_task())
    return True
