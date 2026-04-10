"""
cop/audit.py — Async operator audit trail.

Writes one AuditLog row per write action (fire-and-forget, never raises).
Falls back to stderr logging when the database is not configured.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

log = logging.getLogger("nizam.audit")


async def log_action(
    username: str,
    action: str,
    resource_type: str = "",
    resource_id: str = "",
    detail: Optional[Dict[str, Any]] = None,
    ip: str = "",
    role: str = "ANONYMOUS",
    success: bool = True,
) -> None:
    """
    Write an audit record.  Safe to fire-and-forget — never propagates exceptions.

    Import is deferred so this module can be imported before db is initialised.
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

        async with AsyncSessionLocal() as session:
            record = AuditLog(
                username=username,
                role=role,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                detail=detail or {},
                ip=ip,
                success=1 if success else 0,
            )
            session.add(record)
            await session.commit()

    except Exception as exc:  # pragma: no cover
        log.warning("[audit] DB write failed: %s", exc)
