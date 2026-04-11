"""cop/routers/metrics.py  —  Runtime metrics endpoints (JSON + Prometheus)."""
from __future__ import annotations

import time as _time_mod
from typing import Dict, List

from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse

from ai import deconfliction as ai_deconfliction
from ai import drift as ai_drift  # noqa: F401 — used for drift snapshot
from ai import escalation as ai_escalation
from ai import ew_detector as ai_ew
from ai import ew_ml as ai_ew_ml
from cop import circuit_breaker as cop_cb
from cop import sync as cop_sync
from cop.state import (
    STATE,
    CLIENTS,
    METRICS,
    _METRICS_START_TS,
    AI_ROE_ADVISORIES,
    AI_DRIFT_STATUS,
)

router = APIRouter(tags=["metrics"])


def _percentile(values: List[float], pct: float) -> float:
    """Nearest-rank percentile. Returns 0.0 on empty input."""
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((pct / 100.0) * (len(s) - 1)))))
    return s[k]


@router.get("/api/metrics")
async def api_metrics():
    """Runtime performance metrics: ingest, tactical, WebSocket, state."""
    recent: List[float] = list(METRICS["tactical_recent_ms"])
    uptime_s = _time_mod.time() - _METRICS_START_TS
    ingest_total = METRICS["ingest_total"]
    return JSONResponse({
        "uptime_s": round(uptime_s, 1),
        "ingest": {
            "total":        ingest_total,
            "per_sec":      round(ingest_total / uptime_s, 2) if uptime_s > 0 else 0.0,
            "by_type":      dict(METRICS["ingest_by_type"]),
            "bad_request":  METRICS["ingest_bad_request"],
        },
        "tactical": {
            "scheduled":        METRICS["tactical_scheduled"],
            "ran":              METRICS["tactical_ran"],
            "rate_skipped":     METRICS["tactical_rate_skipped"],
            "overlap_skipped":  METRICS["tactical_overlap_skipped"],
            "failed":           METRICS["tactical_failed"],
            "last_ms":          METRICS["tactical_last_ms"],
            "max_ms":           METRICS["tactical_max_ms"],
            "p50_ms":           round(_percentile(recent, 50), 2),
            "p95_ms":           round(_percentile(recent, 95), 2),
            "p99_ms":           round(_percentile(recent, 99), 2),
            "sample_count":     len(recent),
            "module_ms":        dict(METRICS.get("tactical_module_ms", {})),
        },
        "websocket": {
            "clients":         len(CLIENTS),
            "broadcasts":      METRICS["ws_broadcasts"],
            "messages_sent":   METRICS["ws_messages_sent"],
            "send_failures":   METRICS["ws_send_failures"],
        },
        "state": {
            "tracks":  len(STATE["tracks"]),
            "threats": len(STATE["threats"]),
            "assets":  len(STATE["assets"]),
            "zones":   len(STATE["zones"]),
            "tasks":   len(STATE["tasks"]),
        },
        "deconfliction":    ai_deconfliction.stats(),
        "ew":               ai_ew.stats(),
        "ew_ml":            ai_ew_ml.stats(),
        "sync":             cop_sync.stats(),
        "circuit_breaker":  cop_cb.stats(),
    })


@router.get("/metrics")
async def prometheus_metrics():
    """Prometheus-compatible text metrics (scrape endpoint)."""
    recent: List[float] = list(METRICS["tactical_recent_ms"])
    uptime_s = _time_mod.time() - _METRICS_START_TS
    ingest_total = METRICS["ingest_total"]
    p50  = _percentile(recent, 50)
    p95  = _percentile(recent, 95)
    p99  = _percentile(recent, 99)
    roe_weapons_free   = sum(1 for a in AI_ROE_ADVISORIES if a.get("engagement") == "WEAPONS_FREE")
    roe_weapons_tight  = sum(1 for a in AI_ROE_ADVISORIES if a.get("engagement") == "WEAPONS_TIGHT")
    escalation_pending = len(ai_escalation.get_pending())
    module_ms: Dict[str, float] = METRICS.get("tactical_module_ms", {})

    def g(name: str, help_text: str, value, typ: str = "gauge") -> List[str]:
        return [f"# HELP {name} {help_text}", f"# TYPE {name} {typ}", f"{name} {value}", ""]

    def labeled(name: str, help_text: str, items: Dict[str, float], typ: str = "gauge") -> List[str]:
        out = [f"# HELP {name} {help_text}", f"# TYPE {name} {typ}"]
        for label, val in items.items():
            out.append(f'{name}{{module="{label}"}} {val:.2f}')
        out.append("")
        return out

    lines: List[str] = []
    lines += g("nizam_uptime_seconds",           "Server uptime in seconds",             f"{uptime_s:.1f}")
    lines += g("nizam_ingest_total",              "Total ingested events",                ingest_total, "counter")
    lines += g("nizam_ingest_per_second",         "Current ingest rate events/s",
               f"{ingest_total/uptime_s:.2f}" if uptime_s > 0 else "0")
    lines += g("nizam_ingest_bad_request_total",  "Bad ingest requests",                  METRICS["ingest_bad_request"], "counter")
    lines += g("nizam_tactical_runs_total",       "Total tactical engine runs",           METRICS["tactical_ran"], "counter")
    lines += g("nizam_tactical_failed_total",     "Total tactical engine failures",       METRICS["tactical_failed"], "counter")
    lines += g("nizam_tactical_skipped_total",    "Tactical runs skipped (rate limiter)", METRICS["tactical_rate_skipped"], "counter")
    lines += g("nizam_tactical_p50_ms",           "Tactical engine p50 latency ms",       f"{p50:.2f}")
    lines += g("nizam_tactical_p95_ms",           "Tactical engine p95 latency ms",       f"{p95:.2f}")
    lines += g("nizam_tactical_p99_ms",           "Tactical engine p99 latency ms",       f"{p99:.2f}")
    lines += g("nizam_tactical_max_ms",           "Tactical engine worst-case latency ms",f"{METRICS['tactical_max_ms']:.2f}")
    if module_ms:
        lines += labeled("nizam_tactical_module_ms", "Per-module tactical latency ms (last run)", module_ms)
    lines += g("nizam_ws_clients",               "Connected WebSocket clients",          len(CLIENTS))
    lines += g("nizam_ws_broadcasts_total",       "Total WS broadcasts sent",             METRICS["ws_broadcasts"], "counter")
    lines += g("nizam_ws_messages_total",         "Total WS messages sent",               METRICS["ws_messages_sent"], "counter")
    lines += g("nizam_ws_send_failures_total",    "WS send failures",                     METRICS["ws_send_failures"], "counter")
    lines += g("nizam_tracks",                    "Active track count",                   len(STATE["tracks"]))
    lines += g("nizam_threats",                   "Active threat count",                  len(STATE["threats"]))
    lines += g("nizam_assets",                    "Registered asset count",               len(STATE["assets"]))
    lines += g("nizam_zones",                     "Defined zone count",                   len(STATE["zones"]))
    lines += g("nizam_tasks",                     "Total task count",                     len(STATE["tasks"]))
    lines += g("nizam_roe_advisories",            "Active ROE advisories",                len(AI_ROE_ADVISORIES))
    lines += g("nizam_roe_weapons_free",          "WEAPONS_FREE advisories",              roe_weapons_free)
    lines += g("nizam_roe_weapons_tight",         "WEAPONS_TIGHT advisories",             roe_weapons_tight)
    lines += g("nizam_escalation_pending",        "Unanswered escalation advisories",     escalation_pending)
    lines += g("nizam_ew_alerts_total",           "Total EW alerts detected",             ai_ew.stats().get("total_alerts", 0), "counter")
    lines += g("nizam_deconfliction_merges_total","Total track deconfliction merges",     ai_deconfliction.stats().get("total_aliases", 0), "counter")
    drift_psi     = AI_DRIFT_STATUS.get("psi", 0.0)
    drift_obs     = AI_DRIFT_STATUS.get("observations", 0)
    drift_fp_rate = AI_DRIFT_STATUS.get("fp_rate") or 0.0
    drift_alert   = 1 if AI_DRIFT_STATUS.get("alert") else 0
    lines += g("nizam_model_drift_psi",           "Model drift PSI vs baseline",          f"{drift_psi:.4f}")
    lines += g("nizam_model_drift_observations",  "Confidence observations in window",    drift_obs, "counter")
    lines += g("nizam_model_drift_fp_rate",       "Approximate false-positive rate",      f"{drift_fp_rate:.3f}")
    lines += g("nizam_model_drift_alert",         "1 if drift alert is active",           drift_alert)
    return PlainTextResponse("\n".join(lines), media_type="text/plain; version=0.0.4")
