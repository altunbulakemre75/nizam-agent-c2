"""
cop/helpers.py  —  Tiny helpers shared across server + routers

Kept deliberately minimal: anything with real logic belongs in its own
module, anything route-specific belongs in the router file, and anything
with significant state belongs in cop/state.py. This is strictly for the
two-line utilities that would otherwise create circular imports between
server.py and the router files.
"""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone


def utc_now_iso() -> str:
    """ISO-8601 UTC timestamp string. Used everywhere for 'server_time'."""
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str = "") -> str:
    """Short random id, optionally prefixed (e.g. 'task-', 'zone-')."""
    return f"{prefix}{_uuid.uuid4().hex[:10]}"
