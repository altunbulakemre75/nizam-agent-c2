"""
shared/utils.py — Common utility functions used across agents and adapters.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def wrap_deg(a: float) -> float:
    """Wrap angle to [-180, 180)."""
    return (a + 180.0) % 360.0 - 180.0


def make_envelope(
    event_type: str,
    source_agent: str,
    instance_id: str,
    host: str,
    correlation_id: str,
    payload: dict,
    ts: str | None = None,
) -> dict:
    return {
        "schema_version": "1.1",
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "timestamp": ts or utc_now_iso(),
        "source": {
            "agent_id": source_agent,
            "instance_id": instance_id,
            "host": host,
        },
        "correlation_id": correlation_id,
        "payload": payload,
    }


def parse_ts(ts: str) -> float:
    """Parse ISO-8601 timestamp string to POSIX float."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
