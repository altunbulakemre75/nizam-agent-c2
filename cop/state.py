"""
cop/state.py  —  Central application state module

Houses the live in-memory state that the COP server, its routers, and the
AI engine all share.  Before this module existed, every global was declared
at the top of cop/server.py which made it impossible to extract routers
into separate files (circular imports, implicit coupling).

The goal is NOT to change the shape of state — it is still dict-based,
still mutated in place, still accessed with STATE["tracks"] syntax. The
only behavioural change is *where* the globals live: router files now
`from cop.state import STATE, STATE_LOCK, AI_ML_PREDICTIONS, ...` instead
of reaching into cop.server.

A later refactor can replace these with a dataclass + dependency injection.
For now the priority is breaking the 3600-line file without introducing
semantic drift, and this module is the minimum change that enables it.
"""
from __future__ import annotations

import asyncio
import time as _time_mod
from typing import Any, Dict, List, Set

from starlette.websockets import WebSocket


# ── Core COP state ────────────────────────────────────────────────────────────
STATE: Dict[str, Any] = {
    "agents":           {},
    "tracks":           {},
    "threats":          {},
    "zones":            {},
    "assets":           {},
    "tasks":            {},
    "waypoints":        {},
    "annotations":      {},   # track_id → [list of annotation dicts]
    "events_tail":      [],
    "effector_status":  {},   # {effector_id: {status, updated_at, task_id?, lat?, lon?}}
}

BREACH_STATE: Dict[str, Set[str]] = {}
TASK_EMITTED: Dict[str, Set[str]] = {}
EVENT_TAIL_MAX = 500


# ── Phase 5 — AI state ────────────────────────────────────────────────────────
AI_PREDICTIONS: Dict[str, List[Dict]] = {}   # {track_id: [Kalman predicted points]}
AI_TRAJECTORIES: Dict[str, List[Dict]] = {}  # {track_id: [LSTM predicted waypoints]}
AI_ANOMALIES: List[Dict] = []                # recent anomalies (max 100)
AI_RECOMMENDATIONS: List[Dict] = []           # latest tactical recommendations
AI_PRED_BREACHES: List[Dict] = []             # predictive zone breach warnings
AI_UNCERTAINTY_CONES: Dict[str, List[Dict]] = {}  # uncertainty cones for frontend
AI_COORD_ATTACKS: List[Dict] = []                 # coordinated attack warnings
AI_ROE_ADVISORIES: List[Dict] = []                # ROE engagement advisories
AI_ASSIGNMENT: Dict[str, Any] = {}                # latest effector assignment result
AI_BFT_WARNINGS: List[Dict] = []                  # latest blue-force fratricide warnings
EFFECTOR_OUTCOMES: List[Dict] = []                # recent engagement outcomes (max 50)
AI_DRIFT_STATUS: Dict[str, Any] = {}              # latest model drift status
AI_ML_PREDICTIONS: Dict[str, Dict] = {}           # ML threat predictions per track
AI_ML_PREV_TRACKS: Dict[str, Dict] = {}           # previous frame tracks for acceleration calc
AI_PLUGIN_RESULTS: Dict[str, Any] = {}            # results from ai.registry plugin analyzers
AI_ANOMALY_MAX = 100


# ── WebSocket + locks ─────────────────────────────────────────────────────────
CLIENTS: Set[WebSocket] = set()
CLIENTS_LOCK = asyncio.Lock()
STATE_LOCK = asyncio.Lock()


# ── Multi-operator state ──────────────────────────────────────────────────────
OPERATORS: Dict[str, Dict] = {}          # {operator_id: {joined_at, ws_ref}}
TRACK_CLAIMS: Dict[str, str] = {}        # {track_id: operator_id}
WS_OPERATORS: Dict[int, str] = {}        # id(websocket) → operator_id


# ── Track position history (rolling breadcrumb trail) ────────────────────────
_TRACK_HISTORY_MAX = 50    # max positions kept per track
_track_histories: Dict[str, List[Dict]] = {}


# ── Metrics (in-process counters) ─────────────────────────────────────────────
_METRICS_START_TS: float = _time_mod.time()

METRICS: Dict[str, Any] = {
    # Ingest counters
    "ingest_total":         0,
    "ingest_by_type":       {},          # {"cop.track": N, "cop.threat": N, ...}
    "ingest_bad_request":   0,
    # Tactical engine counters
    "tactical_scheduled":   0,
    "tactical_rate_skipped": 0,
    "tactical_ran":         0,
    "tactical_overlap_skipped": 0,
    "tactical_failed":      0,
    "tactical_last_ms":     0.0,
    "tactical_max_ms":      0.0,
    "tactical_recent_ms":   [],
    "tactical_module_ms":   {},
    # WebSocket fan-out
    "ws_clients":           0,
    "ws_broadcasts":        0,
    "ws_messages_sent":     0,
    "ws_send_failures":     0,
}

_TACTICAL_RECENT_MAX = 32


def make_snapshot_payload() -> Dict[str, Any]:
    """Full state snapshot sent to new WebSocket clients and captured for replay."""
    from cop.helpers import utc_now_iso
    return {
        "tracks":    list(STATE["tracks"].values()),
        "threats":   list(STATE["threats"].values()),
        "zones":     list(STATE["zones"].values()),
        "assets":    list(STATE["assets"].values()),
        "tasks":     [t for t in STATE["tasks"].values() if t["status"] == "PENDING"],
        "waypoints": list(STATE["waypoints"].values()),
        "predictions":       AI_PREDICTIONS,
        "trajectories":      AI_TRAJECTORIES,
        "anomalies":         AI_ANOMALIES[-20:],
        "recommendations":   AI_RECOMMENDATIONS,
        "pred_breaches":     AI_PRED_BREACHES,
        "uncertainty_cones": AI_UNCERTAINTY_CONES,
        "coord_attacks":     AI_COORD_ATTACKS,
        "roe_advisories":    AI_ROE_ADVISORIES,
        "ml_predictions":    AI_ML_PREDICTIONS,
        "server_time": utc_now_iso(),
    }


def metrics_record_tactical_duration(ms: float) -> None:
    """Push a tactical run duration into the rolling window and update max."""
    METRICS["tactical_last_ms"] = round(ms, 2)
    if ms > METRICS["tactical_max_ms"]:
        METRICS["tactical_max_ms"] = round(ms, 2)
    recent: List[float] = METRICS["tactical_recent_ms"]
    recent.append(round(ms, 2))
    if len(recent) > _TACTICAL_RECENT_MAX:
        del recent[: len(recent) - _TACTICAL_RECENT_MAX]


def reset_state() -> None:
    """Wipe in-memory state. Called by /api/reset handler."""
    for key in ("agents", "tracks", "threats", "zones", "assets",
                "tasks", "waypoints", "annotations", "effector_status"):
        STATE[key].clear()
    STATE["events_tail"].clear()
    BREACH_STATE.clear()
    TASK_EMITTED.clear()
    AI_PREDICTIONS.clear()
    AI_TRAJECTORIES.clear()
    AI_ANOMALIES.clear()
    AI_RECOMMENDATIONS.clear()
    AI_PRED_BREACHES.clear()
    AI_UNCERTAINTY_CONES.clear()
    AI_COORD_ATTACKS.clear()
    AI_ROE_ADVISORIES.clear()
    AI_ASSIGNMENT.clear()
    AI_BFT_WARNINGS.clear()
    EFFECTOR_OUTCOMES.clear()
    AI_DRIFT_STATUS.clear()
    AI_ML_PREDICTIONS.clear()
    AI_ML_PREV_TRACKS.clear()
    AI_PLUGIN_RESULTS.clear()
    TRACK_CLAIMS.clear()
    _track_histories.clear()
