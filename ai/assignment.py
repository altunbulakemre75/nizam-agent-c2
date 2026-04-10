"""
ai/assignment.py — Multi-Effector Target Assignment Engine

Uses the Hungarian algorithm (Kuhn-Munkres) to find the globally optimal
assignment of available effectors to active threats, minimising total
engagement cost.

Effectors are friendly assets whose type/capability field contains
"effector", "interceptor", "jammer", or "gun".

Cost model per (threat, effector) pair:
    distance_cost    = dist_km / effector_range_km       (0 → ∞, capped at 2.0)
    priority_cost    = 1.0 - (threat_score / 100)        (0 → 1, high threat = low cost)
    engagement_cost  = 0.0 if engagement_type compatible, else 1.0

    total_cost = 0.5 * distance_cost + 0.5 * priority_cost + engagement_cost

The engine only assigns effectors to tracks that have a WEAPONS_FREE or
WEAPONS_TIGHT ROE advisory.

Usage:
    from ai.assignment import compute

    result = compute(threats, assets, roe_advisories)
    # result: AssignmentResult(assignments=[...], unassigned_threats=[...], stats={})
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Set

import numpy as np
from scipy.optimize import linear_sum_assignment

# ── Constants ──────────────────────────────────────────────────────────────────

DEG_TO_M = 111_320.0

# Default effector range if not specified in asset record
DEFAULT_RANGE_KM = 2.0

# Engagement capability keywords (case-insensitive substring match on asset type/capability)
EFFECTOR_KEYWORDS = {"effector", "interceptor", "jammer", "gun", "laser", "cannon"}

# ROE levels that trigger assignment
ASSIGNABLE_ENGAGEMENTS = {"WEAPONS_FREE", "WEAPONS_TIGHT"}

# Cost cap for distance (beyond this the effector is effectively out of range)
DISTANCE_COST_CAP = 2.0


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class Assignment:
    threat_id:    str
    effector_id:  str
    cost:         float
    dist_km:      float
    threat_score: int
    engagement:   str           # ROE advisory engagement level
    effector_name: str = ""


@dataclass
class AssignmentResult:
    assignments:        List[Assignment] = field(default_factory=list)
    unassigned_threats: List[str]        = field(default_factory=list)
    unassigned_effectors: List[str]      = field(default_factory=list)
    stats: Dict[str, Any]                = field(default_factory=dict)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _dist_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = (lat2 - lat1) * DEG_TO_M
    dlon = (lon2 - lon1) * DEG_TO_M * math.cos(math.radians((lat1 + lat2) / 2))
    return math.sqrt(dlat * dlat + dlon * dlon) / 1000.0


def _is_effector(asset: Dict[str, Any]) -> bool:
    typ = (asset.get("type") or "").lower()
    cap = (asset.get("capability") or "").lower()
    return any(k in typ or k in cap for k in EFFECTOR_KEYWORDS)


def _effector_range_km(asset: Dict[str, Any]) -> float:
    return float(asset.get("range_km") or asset.get("range") or DEFAULT_RANGE_KM)


def _cost(
    threat: Dict[str, Any],
    effector: Dict[str, Any],
    roe_engagement: str,
) -> float:
    tlat, tlon = threat.get("lat") or 0.0, threat.get("lon") or 0.0
    elat, elon = effector.get("lat") or 0.0, effector.get("lon") or 0.0
    range_km = _effector_range_km(effector)
    dist = _dist_km(tlat, tlon, elat, elon)

    distance_cost  = min(dist / range_km, DISTANCE_COST_CAP) if range_km > 0 else DISTANCE_COST_CAP
    score          = int(threat.get("score") or threat.get("threat_score") or 0)
    priority_cost  = 1.0 - (score / 100.0)

    # Prefer WEAPONS_FREE assignments; add small penalty to WEAPONS_TIGHT
    engagement_cost = 0.0 if roe_engagement == "WEAPONS_FREE" else 0.2

    return 0.5 * distance_cost + 0.5 * priority_cost + engagement_cost


# ── Hungarian algorithm (scipy C implementation, O(n³)) ───────────────────────

def _hungarian(cost_matrix: List[List[float]]) -> List[int]:
    """
    Solve the rectangular assignment problem via scipy's C implementation.

    Returns a list of length n_rows where assignment[i] = j means row i → col j,
    or -1 if no assignment. Infinite costs are replaced with a large finite
    sentinel so scipy's solver never refuses a feasible matching.
    """
    n = len(cost_matrix)
    if n == 0:
        return []
    m = len(cost_matrix[0])
    if m == 0:
        return [-1] * n

    C = np.asarray(cost_matrix, dtype=float)

    finite = C[np.isfinite(C)]
    big = float(finite.max()) * 2.0 + 1.0 if finite.size else 1.0
    C = np.where(np.isinf(C), big, C)

    row_ind, col_ind = linear_sum_assignment(C)

    result = [-1] * n
    for r, c in zip(row_ind, col_ind):
        if r < n and c < m:
            result[int(r)] = int(c)
    return result


# ── Public API ─────────────────────────────────────────────────────────────────

def compute(
    threats:        Dict[str, Dict[str, Any]],
    assets:         Dict[str, Dict[str, Any]],
    roe_advisories: List[Dict[str, Any]],
) -> AssignmentResult:
    """
    Compute the optimal effector-to-threat assignment.

    Only threats with WEAPONS_FREE or WEAPONS_TIGHT ROE advisories are
    considered. Only assets flagged as effectors (see EFFECTOR_KEYWORDS)
    are used.

    Returns an AssignmentResult with the optimal assignment list and
    unassigned threat/effector lists.
    """
    # Build ROE map: track_id → engagement level
    roe_map: Dict[str, str] = {}
    for adv in roe_advisories:
        eng = adv.get("engagement", "")
        if eng in ASSIGNABLE_ENGAGEMENTS:
            roe_map[adv["track_id"]] = eng

    # Filter assignable threats
    threat_ids = [tid for tid in roe_map if tid in threats]
    if not threat_ids:
        return AssignmentResult(
            unassigned_threats=list(roe_map.keys()),
            stats={"threats": 0, "effectors": 0, "assigned": 0},
        )

    # Filter active effectors
    effector_ids = [
        aid for aid, a in assets.items()
        if _is_effector(a)
        and a.get("status", "active") == "active"
        and a.get("lat") is not None
    ]
    if not effector_ids:
        return AssignmentResult(
            unassigned_threats=threat_ids,
            stats={"threats": len(threat_ids), "effectors": 0, "assigned": 0},
        )

    # Build cost matrix [threats × effectors]
    cost_matrix: List[List[float]] = []
    for tid in threat_ids:
        threat = threats[tid]
        row = []
        for eid in effector_ids:
            effector = assets[eid]
            row.append(_cost(threat, effector, roe_map[tid]))
        cost_matrix.append(row)

    # Solve with Hungarian algorithm
    assignment_indices = _hungarian(cost_matrix)

    assignments: List[Assignment] = []
    assigned_threat_ids: Set[str] = set()
    assigned_effector_ids: Set[str] = set()

    for ti, ej in enumerate(assignment_indices):
        if ej < 0 or ej >= len(effector_ids):
            continue
        tid      = threat_ids[ti]
        eid      = effector_ids[ej]
        cost     = cost_matrix[ti][ej]
        threat   = threats[tid]
        effector = assets[eid]
        tlat, tlon = threat.get("lat") or 0.0, threat.get("lon") or 0.0
        elat, elon = effector.get("lat") or 0.0, effector.get("lon") or 0.0
        d_km     = _dist_km(tlat, tlon, elat, elon)

        # Skip assignments where target is beyond effector's physical range
        if d_km > _effector_range_km(effector):
            continue

        assignments.append(Assignment(
            threat_id    = tid,
            effector_id  = eid,
            cost         = round(cost, 3),
            dist_km      = round(d_km, 2),
            threat_score = int(threat.get("score") or threat.get("threat_score") or 0),
            engagement   = roe_map[tid],
            effector_name= effector.get("name") or eid,
        ))
        assigned_threat_ids.add(tid)
        assigned_effector_ids.add(eid)

    unassigned_threats   = [t for t in threat_ids   if t not in assigned_threat_ids]
    unassigned_effectors = [e for e in effector_ids if e not in assigned_effector_ids]

    return AssignmentResult(
        assignments          = assignments,
        unassigned_threats   = unassigned_threats,
        unassigned_effectors = unassigned_effectors,
        stats = {
            "threats":          len(threat_ids),
            "effectors":        len(effector_ids),
            "assigned":         len(assignments),
            "unassigned_t":     len(unassigned_threats),
            "unassigned_e":     len(unassigned_effectors),
        },
    )
