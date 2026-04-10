"""
cop/webhooks.py — Outbound webhook dispatcher for NIZAM COP.

Sends HTTP POST notifications to registered URLs when critical events occur:
  - HIGH threat detected / escalated
  - Zone breach
  - EW attack detected

Features:
  - Async, non-blocking (events queued, worker drains in background)
  - Per-URL retry with exponential back-off (max 3 attempts)
  - In-memory registry + optional WEBHOOK_URL env var for quick setup
  - Payload schema: {event_type, timestamp, node_id, payload}

Configuration:
  WEBHOOK_URL   Comma-separated URLs to register at startup (optional).
                Example: WEBHOOK_URL=https://hooks.slack.com/services/xxx

Usage (server.py):
  from cop import webhooks
  webhooks.register("https://hooks.slack.com/services/xxx")
  await webhooks.dispatch("cop.threat_high", {"track_id": ..., "level": "HIGH"})
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Set

log = logging.getLogger("nizam.webhooks")

# ── Registry ──────────────────────────────────────────────────────────────────

_URLS: Set[str] = set()

# Seed from env on import
for _u in os.environ.get("WEBHOOK_URL", "").split(","):
    _u = _u.strip()
    if _u:
        _URLS.add(_u)

_QUEUE: asyncio.Queue  # initialised lazily on first dispatch
_WORKER_TASK: asyncio.Task | None = None
_NODE_ID = os.environ.get("NODE_ID", "cop-node-01")


def register(url: str) -> bool:
    """Add a webhook URL. Returns True if newly added."""
    url = url.rstrip("/")
    if url in _URLS:
        return False
    _URLS.add(url)
    log.info("[webhook] registered: %s  (total %d)", url, len(_URLS))
    return True


def unregister(url: str) -> bool:
    """Remove a webhook URL. Returns True if it existed."""
    url = url.rstrip("/")
    try:
        _URLS.discard(url)
        log.info("[webhook] removed: %s", url)
        return True
    except KeyError:
        return False


def list_webhooks() -> List[str]:
    return sorted(_URLS)


# ── Dispatch ──────────────────────────────────────────────────────────────────

async def dispatch(event_type: str, payload: Dict[str, Any]) -> None:
    """
    Enqueue an event for delivery to all registered URLs.
    Returns immediately — delivery happens in the background worker.
    """
    if not _URLS:
        return

    global _QUEUE, _WORKER_TASK

    # Lazy init — must run inside an asyncio event loop
    if not hasattr(dispatch, "_initialised"):
        _QUEUE = asyncio.Queue(maxsize=500)
        dispatch._initialised = True  # type: ignore[attr-defined]

    if _WORKER_TASK is None or _WORKER_TASK.done():
        _WORKER_TASK = asyncio.create_task(_worker())

    envelope = {
        "event_type": event_type,
        "timestamp":  _utc_now(),
        "node_id":    _NODE_ID,
        "payload":    payload,
    }
    try:
        _QUEUE.put_nowait(envelope)
    except asyncio.QueueFull:
        log.warning("[webhook] queue full, dropping %s", event_type)


# ── Worker ────────────────────────────────────────────────────────────────────

async def _worker() -> None:
    """Background task: drain queue and POST to all URLs with retry."""
    while True:
        try:
            envelope = await asyncio.wait_for(_QUEUE.get(), timeout=5.0)
        except asyncio.TimeoutError:
            continue

        urls = list(_URLS)  # snapshot — URLs may change during delivery
        data = json.dumps(envelope, ensure_ascii=False, default=str).encode()

        for url in urls:
            asyncio.create_task(_post_with_retry(url, data, envelope["event_type"]))

        _QUEUE.task_done()


async def _post_with_retry(url: str, data: bytes, event_type: str, max_attempts: int = 3) -> None:
    """POST `data` to `url` with exponential back-off."""
    for attempt in range(1, max_attempts + 1):
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, _sync_post, url, data
            )
            log.debug("[webhook] delivered %s → %s", event_type, url)
            return
        except Exception as exc:
            wait = 2 ** attempt
            log.warning(
                "[webhook] attempt %d/%d failed for %s → %s: %s (retry in %ds)",
                attempt, max_attempts, event_type, url, exc, wait,
            )
            if attempt < max_attempts:
                await asyncio.sleep(wait)

    log.error("[webhook] giving up on %s → %s after %d attempts", event_type, url, max_attempts)


def _sync_post(url: str, data: bytes) -> None:
    """Blocking POST — runs in executor thread."""
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent":   "NIZAM-COP/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        status = resp.status
        if status >= 400:
            raise ValueError(f"HTTP {status}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
