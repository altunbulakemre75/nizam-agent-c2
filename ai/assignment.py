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


# ── Hungarian algorithm (O(n³)) ────────────────────────────────────────────────

def _hungarian(cost_matrix: List[List[float]]) -> List[int]:
    """
    Solve the assignment problem using the Hungarian algorithm.

    Parameters
    ----------
    cost_matrix : n_rows × n_cols matrix (n_rows ≤ n_cols required; pad if necessary)

    Returns
    -------
    assignment : list of length n_rows where assignment[i] = j means row i → col j,
                 or -1 if no assignment found for row i.
    """
    n = len(cost_matrix)
    if n == 0:
        return []
    m = len(cost_matrix[0])
    if m == 0:
        return [-1] * n

    # Pad to square matrix (n × n, n = max(n_rows, n_cols))
    size = max(n, m)
    INF  = float("inf")
    C: List[List[float]] = []
    for i in range(size):
        row = []
        for j in range(size):
            if i < n and j < m:
                row.append(cost_matrix[i][j])
            else:
                row.append(INF)
        C.append(row)

    # Replace INF with a large finite number for arithmetic stability
    big = max(v for row in C for v in row if v != INF) * 2 + 1 if any(
        v != INF for row in C for v in row
    ) else 1.0
    C = [[big if v == INF else v for v in row] for row in C]

    # Step 1: subtract row minimum
    for i in range(size):
        mn = min(C[i])
        C[i] = [v - mn for v in C[i]]

    # Step 2: subtract column minimum
    for j in range(size):
        mn = min(C[i][j] for i in range(size))
        for i in range(size):
            C[i][j] -= mn

    MAX_ITER = size * 4

    for _ in range(MAX_ITER):
        # Try to find a complete matching with zeros
        row_covered: List[bool] = [False] * size
        col_covered: List[bool] = [False] * size
        starred: List[List[bool]] = [[False] * size for _ in range(size)]
        primed:  List[List[bool]] = [[False] * size for _ in range(size)]

        # Star one zero in each uncovered row/col
        for i in range(size):
            for j in range(size):
                if C[i][j] == 0 and not row_covered[i] and not col_covered[j]:
                    starred[i][j]  = True
                    row_covered[i] = True
                    col_covered[j] = True

        row_covered = [False] * size
        col_covered = [False] * size

        # Cover columns with starred zeros
        for j in range(size):
            if any(starred[i][j] for i in range(size)):
                col_covered[j] = True

        done = sum(col_covered) == size
        while not done:
            # Find uncovered zero; prime it
            found = False
            pi, pj = -1, -1
            for i in range(size):
                if row_covered[i]:
                    continue
                for j in range(size):
                    if C[i][j] == 0 and not col_covered[j]:
                        primed[i][j] = True
                        pi, pj = i, j
                        found = True
                        break
                if found:
                    break

            if not found:
                # No uncovered zero — update matrix
                min_val = big
                for i in range(size):
                    for j in range(size):
                        if not row_covered[i] and not col_covered[j]:
                            if C[i][j] < min_val:
                                min_val = C[i][j]
                if min_val == big:
                    break
                for i in range(size):
                    for j in range(size):
                        if row_covered[i]:
                            C[i][j] += min_val
                        if not col_covered[j]:
                            C[i][j] -= min_val
                continue

            # Starred zero in row pi?
            sj = next((j for j in range(size) if starred[pi][j]), -1)
            if sj >= 0:
                row_covered[pi] = True
                col_covered[sj] = False
            else:
                # Augment along alternating path
                path = [(pi, pj)]
                while True:
                    last_j = path[-1][1]
                    si = next((i for i in range(size) if starred[i][last_j]), -1)
                    if si < 0:
                        break
                    path.append((si, last_j))
                    last_i = path[-1][0]
                    pj2 = next((j for j in range(size) if primed[last_i][j]), -1)
                    if pj2 < 0:
                        break
                    path.append((last_i, pj2))
                for (r, c) in path:
                    if starred[r][c]:
                        starred[r][c] = False
                    elif primed[r][c]:
                        starred[r][c] = True

                row_covered  = [False] * size
                col_covered  = [False] * size
                primed       = [[False] * size for _ in range(size)]

                for j in range(size):
                    if any(starred[i][j] for i in range(size)):
                        col_covered[j] = True
                done = sum(col_covered) == size

        # Extract assignment from starred zeros
        result = [-1] * size
        for i in range(size):
            for j in range(size):
                if starred[i][j]:
                    result[i] = j
                    break
        # Return only valid rows
        return result[:n]

    return [-1] * n


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
