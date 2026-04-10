"""
cop/audit.py — Async operator audit trail with SHA-256 hash chain.

Writes one AuditLog row per write action. Every row carries:
    prev_hash  — entry_hash of the immediately preceding row (or "" for genesis)
    entry_hash — sha256(canonical_json || prev_hash)

Altering or removing any past row breaks the chain on every row after it, so a
compliance verifier can replay the log and detect tampering. See verify_chain()
at the bottom of this file for the replay implementation.

The chain head is cached in memory and seeded from the DB on first use so it
survives restarts. If the DB is unavailable, log_action() still emits the
application log line and the module silently degrades to no-op.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("nizam.audit")

# ── Chain state (in-memory cache of most recent entry_hash) ──────────────────

_chain_lock: asyncio.Lock = asyncio.Lock()
_last_hash: Optional[str] = None        # None = not yet seeded from DB
_seeded:    bool          = False

# Genesis sentinel — the "previous hash" of the very first record. Chosen to
# be recognisable in dumps; any constant works as long as verify_chain uses
# the same seed.
GENESIS_PREV_HASH = ""


# ── Canonical serialization ───────────────────────────────────────────────────

def _canonical_repr(
    time_iso:      str,
    username:      str,
    role:          str,
    action:        str,
    resource_type: str,
    resource_id:   str,
    detail:        Dict[str, Any],
    ip:            str,
    success:       bool,
) -> str:
    """
    Deterministic JSON representation of a record. Keys sorted, no whitespace,
    so two verifiers compute the same hash regardless of dict insertion order
    or dump formatting.
    """
    body = {
        "time":          time_iso or "",
        "username":      username or "",
        "role":          role or "",
        "action":        action or "",
        "resource_type": resource_type or "",
        "resource_id":   resource_id or "",
        "detail":        detail or {},
        "ip":            ip or "",
        "success":       1 if success else 0,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)


def compute_entry_hash(canonical: str, prev_hash: str) -> str:
    """sha256(canonical_repr || prev_hash) — hex digest."""
    h = hashlib.sha256()
    h.update(canonical.encode("utf-8"))
    h.update(b"|")
    h.update((prev_hash or "").encode("utf-8"))
    return h.hexdigest()


# ── Seeding from DB ───────────────────────────────────────────────────────────

async def _seed_last_hash() -> None:
    """Load the most recent entry_hash from the DB once per process."""
    global _last_hash, _seeded
    if _seeded:
        return
    try:
        from sqlalchemy import select

        from db.models import AuditLog
        from db.session import AsyncSessionLocal

        if AsyncSessionLocal is None:
            _last_hash = None
            _seeded = True
            return

        async with AsyncSessionLocal() as session:
            row = (await session.execute(
                select(AuditLog.entry_hash)
                .order_by(AuditLog.time.desc())
                .limit(1)
            )).scalar_one_or_none()
            _last_hash = row  # may be None for empty table
    except Exception as exc:  # pragma: no cover
        log.warning("[audit] seed failed: %s", exc)
        _last_hash = None
    finally:
        _seeded = True


# ── Public API ────────────────────────────────────────────────────────────────

async def log_action(
    username:      str,
    action:        str,
    resource_type: str = "",
    resource_id:   str = "",
    detail:        Optional[Dict[str, Any]] = None,
    ip:            str = "",
    role:          str = "ANONYMOUS",
    success:       bool = True,
) -> None:
    """
    Write an audit record. Safe to fire-and-forget — never propagates
    exceptions. Each write links to the previous one via entry_hash.
    """
    # Always log to application log so audit trail survives DB outage
    log.info(
        "[audit] user=%s role=%s action=%s %s/%s ip=%s ok=%s",
        username, role, action, resource_type, resource_id, ip, success,
    )

    try:
        from db.models import AuditLog
        from db.session import AsyncSessionLocal

        if AsyncSessionLocal is None:
            return

        # Seed chain head on first use
        await _seed_last_hash()

        time_iso = datetime.now(timezone.utc).isoformat()
        canonical = _canonical_repr(
            time_iso, username, role, action,
            resource_type, resource_id, detail or {}, ip, success,
        )

        async with _chain_lock:
            global _last_hash
            prev = _last_hash if _last_hash is not None else GENESIS_PREV_HASH
            entry_hash = compute_entry_hash(canonical, prev)

            async with AsyncSessionLocal() as session:
                record = AuditLog(
                    username      = username,
                    role          = role,
                    action        = action,
                    resource_type = resource_type,
                    resource_id   = resource_id,
                    detail        = detail or {},
                    ip            = ip,
                    success       = 1 if success else 0,
                    prev_hash     = prev,
                    entry_hash    = entry_hash,
                )
                session.add(record)
                await session.commit()

            _last_hash = entry_hash

    except Exception as exc:  # pragma: no cover
        log.warning("[audit] DB write failed: %s", exc)


def reset_chain_cache() -> None:
    """For tests / /api/reset: force the next write to re-seed from DB."""
    global _last_hash, _seeded
    _last_hash = None
    _seeded = False


# ── Verification ──────────────────────────────────────────────────────────────

def verify_chain(records: List[Dict[str, Any]]) -> Tuple[bool, Optional[int], str]:
    """
    Replay the hash chain over a list of records and return
    (ok, first_bad_index, message).

    Each record must be a dict-like with fields:
        time, username, role, action, resource_type, resource_id,
        detail, ip, success, prev_hash, entry_hash
    (order doesn't matter — this is what you get from SQLAlchemy row._mapping).

    Intended usage: an auditor dumps the audit_logs table ordered by time
    and calls this to prove nothing was silently mutated.
    """
    prev = GENESIS_PREV_HASH
    for i, r in enumerate(records):
        t = r.get("time")
        time_iso = t.isoformat() if isinstance(t, datetime) else (t or "")
        canonical = _canonical_repr(
            time_iso,
            r.get("username", ""),
            r.get("role", "") or "",
            r.get("action", ""),
            r.get("resource_type", "") or "",
            r.get("resource_id", "") or "",
            r.get("detail") or {},
            r.get("ip", "") or "",
            bool(r.get("success")),
        )
        if (r.get("prev_hash") or "") != prev:
            return False, i, f"prev_hash mismatch at index {i}"

        expected = compute_entry_hash(canonical, prev)
        if r.get("entry_hash") != expected:
            return False, i, f"entry_hash mismatch at index {i}"

        prev = expected

    return True, None, f"OK — verified {len(records)} records"
