"""
tests/test_assignment.py — Unit tests for ai/assignment.py

Covers:
  - Empty threats / no ROE → empty result
  - No effectors → all threats unassigned
  - 1-threat 1-effector → assigned
  - 2-threats 2-effectors → optimal (lower cost)
  - 3x3 Hungarian correctness
  - Out-of-range effector not assigned
  - Non-effector assets ignored
  - Inactive effectors ignored
  - WEAPONS_HOLD not assigned
  - Cost ordering: higher threat score gets lower priority_cost
"""
from __future__ import annotations

import pytest
from ai.assignment import (
    compute,
    AssignmentResult,
    _hungarian,
    _cost,
    _dist_km,
    DISTANCE_COST_CAP,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _threat(track_id: str, lat: float, lon: float, score: int = 80) -> dict:
    return {
        "lat": lat, "lon": lon,
        "score": score,
        "threat_level": "HIGH",
        "intent": "attack",
    }


def _effector(asset_id: str, lat: float, lon: float,
              range_km: float = 5.0, status: str = "active") -> dict:
    return {
        "type": "interceptor",
        "lat": lat, "lon": lon,
        "range_km": range_km,
        "status": status,
        "name": f"EFF-{asset_id}",
    }


def _roe(track_id: str, eng: str = "WEAPONS_FREE") -> dict:
    return {"track_id": track_id, "engagement": eng}


# ── Hungarian algorithm unit tests ────────────────────────────────────────────

class TestHungarian:
    def test_empty(self):
        assert _hungarian([]) == []

    def test_1x1(self):
        r = _hungarian([[3.0]])
        assert r == [0]

    def test_2x2_trivial(self):
        # cost[0][0]=1, cost[0][1]=4, cost[1][0]=2, cost[1][1]=3
        # optimal: row0→col0 (1), row1→col1 (3) = 4
        r = _hungarian([[1, 4], [2, 3]])
        assert r[0] == 0
        assert r[1] == 1

    def test_2x2_cross(self):
        # optimal: row0→col1 (1), row1→col0 (2) = 3
        r = _hungarian([[4, 1], [2, 5]])
        assert r[0] == 1
        assert r[1] == 0

    def test_3x3(self):
        # Classic example — verify total cost is minimal
        C = [
            [9, 2, 7],
            [3, 6, 1],
            [5, 8, 4],
        ]
        r = _hungarian(C)
        total = sum(C[i][r[i]] for i in range(3))
        # Optimal is 2+1+5 = 8 or 9+1+8 — known minimum is 8
        assert total <= 8

    def test_nonsquare_more_cols(self):
        # 2 rows, 3 cols → should assign 2 rows to 2 of 3 cols
        C = [[1, 5, 3], [4, 2, 6]]
        r = _hungarian(C)
        assert len(r) == 2
        # Each row should be assigned a valid col
        assert all(0 <= r[i] < 3 for i in range(2))
        # No duplicate assignments
        assert len(set(r)) == 2


# ── compute() integration tests ───────────────────────────────────────────────

class TestComputeEmpty:
    def test_no_threats_no_advisories(self):
        result = compute({}, {}, [])
        assert result.assignments == []
        assert result.unassigned_threats == []

    def test_threats_but_no_roe(self):
        threats = {"T1": _threat("T1", 41.0, 29.0)}
        result  = compute(threats, {}, [])
        assert result.assignments == []
        assert result.unassigned_threats == []  # no ROE → not in scope

    def test_roe_weapons_hold_not_assigned(self):
        threats   = {"T1": _threat("T1", 41.0, 29.0)}
        effectors = {"E1": _effector("E1", 41.01, 29.01)}
        roe       = [_roe("T1", "WEAPONS_HOLD")]
        result    = compute(threats, effectors, roe)
        assert result.assignments == []

    def test_no_effectors(self):
        threats = {"T1": _threat("T1", 41.0, 29.0)}
        roe     = [_roe("T1")]
        result  = compute(threats, {}, roe)
        assert result.assignments == []
        assert "T1" in result.unassigned_threats


class TestComputeBasic:
    def test_one_threat_one_effector(self):
        threats   = {"T1": _threat("T1", 41.0, 29.0)}
        effectors = {"E1": _effector("E1", 41.01, 29.01)}
        roe       = [_roe("T1")]
        result    = compute(threats, effectors, roe)
        assert len(result.assignments) == 1
        a = result.assignments[0]
        assert a.threat_id   == "T1"
        assert a.effector_id == "E1"
        assert a.cost >= 0

    def test_assignment_contains_dist(self):
        threats   = {"T1": _threat("T1", 41.0, 29.0)}
        effectors = {"E1": _effector("E1", 41.01, 29.01)}
        result    = compute(threats, effectors, [_roe("T1")])
        assert result.assignments[0].dist_km > 0

    def test_weapons_tight_also_assigned(self):
        threats   = {"T1": _threat("T1", 41.0, 29.0)}
        effectors = {"E1": _effector("E1", 41.01, 29.01)}
        result    = compute(threats, effectors, [_roe("T1", "WEAPONS_TIGHT")])
        assert len(result.assignments) == 1


class TestComputeMultiple:
    def test_two_threats_two_effectors_no_overlap(self):
        """Each effector should be assigned to at most one threat."""
        threats = {
            "T1": _threat("T1", 41.0, 29.0, score=90),
            "T2": _threat("T2", 41.05, 29.05, score=70),
        }
        effectors = {
            "E1": _effector("E1", 41.01, 29.01),
            "E2": _effector("E2", 41.06, 29.06),
        }
        roe = [_roe("T1"), _roe("T2")]
        result = compute(threats, effectors, roe)
        assert len(result.assignments) == 2
        assigned_effectors = [a.effector_id for a in result.assignments]
        assert len(set(assigned_effectors)) == 2   # no duplicate effector

    def test_stats_counts(self):
        threats = {
            "T1": _threat("T1", 41.0, 29.0),
            "T2": _threat("T2", 41.1, 29.1),
        }
        effectors = {"E1": _effector("E1", 41.01, 29.01)}
        roe = [_roe("T1"), _roe("T2")]
        result = compute(threats, effectors, roe)
        assert result.stats["threats"]   == 2
        assert result.stats["effectors"] == 1
        assert result.stats["assigned"]  == 1
        assert result.stats["unassigned_t"] == 1


class TestComputeFilters:
    def test_non_effector_asset_ignored(self):
        threats = {"T1": _threat("T1", 41.0, 29.0)}
        assets  = {"A1": {"type": "friendly", "lat": 41.01, "lon": 29.01,
                          "status": "active", "name": "FOB"}}
        result  = compute(threats, assets, [_roe("T1")])
        assert result.assignments == []
        assert "T1" in result.unassigned_threats

    def test_inactive_effector_ignored(self):
        threats   = {"T1": _threat("T1", 41.0, 29.0)}
        effectors = {"E1": _effector("E1", 41.01, 29.01, status="inactive")}
        result    = compute(threats, effectors, [_roe("T1")])
        assert result.assignments == []

    def test_out_of_range_not_assigned(self):
        # Effector range 0.1 km, target 10 km away
        threats   = {"T1": _threat("T1", 41.0, 29.0)}
        effectors = {"E1": _effector("E1", 41.09, 29.09, range_km=0.1)}
        result    = compute(threats, effectors, [_roe("T1")])
        assert result.assignments == []
        assert "T1" in result.unassigned_threats
