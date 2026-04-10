"""
tests/test_audit_chain.py — SHA-256 hash chain for the audit log

These tests pin the tamper-evidence contract added in A2:
  - Canonical serialisation is stable across dict insertion order.
  - Each entry hashes the canonical repr + the previous entry_hash.
  - verify_chain() accepts untampered sequences, rejects mutated ones, and
    rejects deletions.

Pure unit tests — no DB, no FastAPI. The DB write path is a thin wrapper
around compute_entry_hash which is covered here.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cop import audit as cop_audit


def _make_record(
    *,
    time,
    username="alice",
    role="OPERATOR",
    action="approve",
    resource_type="task",
    resource_id="T-1",
    detail=None,
    ip="10.0.0.1",
    success=True,
    prev_hash="",
    entry_hash=None,
):
    rec = {
        "time":          time,
        "username":      username,
        "role":          role,
        "action":        action,
        "resource_type": resource_type,
        "resource_id":   resource_id,
        "detail":        detail or {},
        "ip":            ip,
        "success":       success,
        "prev_hash":     prev_hash,
    }
    canonical = cop_audit._canonical_repr(
        time.isoformat() if isinstance(time, datetime) else time,
        username, role, action, resource_type, resource_id,
        detail or {}, ip, success,
    )
    rec["entry_hash"] = entry_hash if entry_hash is not None else \
        cop_audit.compute_entry_hash(canonical, prev_hash)
    return rec


def _link_chain(records):
    """Relink records so each prev_hash/entry_hash follows from the previous."""
    prev = cop_audit.GENESIS_PREV_HASH
    out = []
    for r in records:
        rec = dict(r)
        rec["prev_hash"] = prev
        canonical = cop_audit._canonical_repr(
            rec["time"].isoformat() if isinstance(rec["time"], datetime) else rec["time"],
            rec["username"], rec["role"], rec["action"],
            rec["resource_type"], rec["resource_id"],
            rec["detail"] or {}, rec["ip"], rec["success"],
        )
        rec["entry_hash"] = cop_audit.compute_entry_hash(canonical, prev)
        prev = rec["entry_hash"]
        out.append(rec)
    return out


class TestCanonicalRepr:
    def test_repr_stable_under_key_order(self):
        t = datetime(2026, 4, 11, 12, 0, 0, tzinfo=timezone.utc).isoformat()
        a = cop_audit._canonical_repr(
            t, "alice", "OPERATOR", "approve", "task", "T-1",
            {"b": 2, "a": 1}, "10.0.0.1", True,
        )
        b = cop_audit._canonical_repr(
            t, "alice", "OPERATOR", "approve", "task", "T-1",
            {"a": 1, "b": 2}, "10.0.0.1", True,
        )
        assert a == b

    def test_repr_distinguishes_content(self):
        t = datetime(2026, 4, 11, 12, 0, 0, tzinfo=timezone.utc).isoformat()
        base_kwargs = dict(
            time_iso=t, username="alice", role="OPERATOR", action="approve",
            resource_type="task", resource_id="T-1", detail={}, ip="10.0.0.1", success=True,
        )
        a = cop_audit._canonical_repr(**base_kwargs)
        variations = [
            {**base_kwargs, "username": "bob"},
            {**base_kwargs, "action": "reject"},
            {**base_kwargs, "resource_id": "T-2"},
            {**base_kwargs, "success": False},
            {**base_kwargs, "detail": {"note": "x"}},
        ]
        for v in variations:
            assert cop_audit._canonical_repr(**v) != a


class TestComputeEntryHash:
    def test_hash_is_deterministic(self):
        canon = '{"a":1}'
        h1 = cop_audit.compute_entry_hash(canon, "abc")
        h2 = cop_audit.compute_entry_hash(canon, "abc")
        assert h1 == h2
        assert len(h1) == 64

    def test_hash_changes_with_prev(self):
        canon = '{"a":1}'
        assert cop_audit.compute_entry_hash(canon, "") != \
               cop_audit.compute_entry_hash(canon, "xxx")

    def test_genesis_empty_prev_is_valid(self):
        h = cop_audit.compute_entry_hash("{}", cop_audit.GENESIS_PREV_HASH)
        assert isinstance(h, str) and len(h) == 64


class TestVerifyChain:
    def _make_three(self):
        base = datetime(2026, 4, 11, 12, 0, 0, tzinfo=timezone.utc)
        return _link_chain([
            _make_record(time=base, action="approve",  resource_id="T-1"),
            _make_record(time=base, action="reject",   resource_id="T-2"),
            _make_record(time=base, action="engage",   resource_id="T-3"),
        ])

    def test_empty_chain_is_valid(self):
        ok, bad, msg = cop_audit.verify_chain([])
        assert ok is True
        assert bad is None
        assert "0" in msg

    def test_valid_chain_passes(self):
        ok, bad, msg = cop_audit.verify_chain(self._make_three())
        assert ok is True
        assert bad is None

    def test_tampered_action_detected(self):
        chain = self._make_three()
        chain[1]["action"] = "approve"
        ok, bad, _ = cop_audit.verify_chain(chain)
        assert ok is False
        assert bad == 1

    def test_tampered_detail_detected(self):
        chain = self._make_three()
        chain[0]["detail"] = {"added": "hack"}
        ok, bad, _ = cop_audit.verify_chain(chain)
        assert ok is False
        assert bad == 0

    def test_deleted_record_breaks_chain(self):
        chain = self._make_three()
        del chain[1]
        ok, bad, _ = cop_audit.verify_chain(chain)
        assert ok is False
        assert bad == 1

    def test_swapped_records_break_chain(self):
        chain = self._make_three()
        chain[0], chain[1] = chain[1], chain[0]
        ok, bad, _ = cop_audit.verify_chain(chain)
        assert ok is False
        assert bad in (0, 1)

    def test_substituted_entry_hash_detected(self):
        chain = self._make_three()
        chain[1]["entry_hash"] = "0" * 64
        ok, bad, _ = cop_audit.verify_chain(chain)
        assert ok is False
        assert bad == 1

    def test_chain_survives_unicode_and_none_fields(self):
        base = datetime(2026, 4, 11, tzinfo=timezone.utc)
        chain = _link_chain([
            _make_record(time=base, username="caglar", detail={"note": "suru tespiti"}),
            _make_record(time=base, role=None, resource_type=None),
        ])
        ok, bad, _ = cop_audit.verify_chain(chain)
        assert ok is True
        assert bad is None
