"""cop/routers/audit.py  —  Audit log read + hash-chain verification."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from cop import audit as cop_audit

try:
    from auth.deps import require_admin
    from db.session import get_db
    _DB_OK = True
except Exception:
    def require_admin():
        return lambda: None
    def get_db():
        yield None
    _DB_OK = False

router = APIRouter(tags=["system"])


@router.get("/api/audit")
async def api_audit(
    limit:         int = Query(100, ge=1, le=1000),
    offset:        int = Query(0, ge=0),
    username:      Optional[str] = Query(None),
    action:        Optional[str] = Query(None),
    resource_type: Optional[str] = Query(None),
    _=Depends(require_admin()),
    db=Depends(get_db),
):
    """Admin-only: paginated audit log with optional filters."""
    if db is None:
        return JSONResponse({"records": [], "total": 0, "note": "DB not configured"})

    from sqlalchemy import select, func
    from db.models import AuditLog

    stmt = select(AuditLog).order_by(AuditLog.time.desc())
    if username:
        stmt = stmt.where(AuditLog.username == username)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if resource_type:
        stmt = stmt.where(AuditLog.resource_type == resource_type)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar() or 0

    stmt = stmt.offset(offset).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()

    records = [
        {
            "time":          r.time.isoformat() if r.time else None,
            "username":      r.username,
            "role":          r.role,
            "action":        r.action,
            "resource_type": r.resource_type,
            "resource_id":   r.resource_id,
            "detail":        r.detail,
            "ip":            r.ip,
            "success":       bool(r.success),
            "prev_hash":     r.prev_hash,
            "entry_hash":    r.entry_hash,
        }
        for r in rows
    ]
    return JSONResponse({"records": records, "total": total, "offset": offset, "limit": limit})


@router.get("/api/audit/verify")
async def api_audit_verify(
    _=Depends(require_admin()),
    db=Depends(get_db),
):
    """
    Admin-only: replay the audit hash chain in time order and report whether
    the chain is intact. Use this to prove to an auditor that the log has not
    been silently mutated.
    """
    if db is None:
        return JSONResponse({"ok": False, "error": "DB not configured"}, status_code=503)

    from sqlalchemy import select
    from db.models import AuditLog

    rows = (await db.execute(
        select(AuditLog).order_by(AuditLog.time.asc(), AuditLog.id.asc())
    )).scalars().all()

    records = [
        {
            "time":          r.time,
            "username":      r.username,
            "role":          r.role,
            "action":        r.action,
            "resource_type": r.resource_type,
            "resource_id":   r.resource_id,
            "detail":        r.detail,
            "ip":            r.ip,
            "success":       bool(r.success),
            "prev_hash":     r.prev_hash,
            "entry_hash":    r.entry_hash,
        }
        for r in rows
    ]

    ok, bad_index, message = cop_audit.verify_chain(records)
    return JSONResponse({
        "ok":          ok,
        "message":     message,
        "total":       len(records),
        "first_bad":   bad_index,
    })
