"""
cop/ratelimit.py — In-memory sliding-window rate limiter middleware.

Configuration (environment variables):
  RATE_LIMIT_WRITES   max write requests per IP per minute   (default: 200)
  RATE_LIMIT_WINDOW_S sliding window duration in seconds      (default: 60)

Only POST/PUT/DELETE/PATCH requests are counted.
Exempt paths: /api/ingest (has its own limiter), /api/sync/receive, /auth/login
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Dict, List, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

# ── Configuration ─────────────────────────────────────────────────────────────

_MAX_REQUESTS  = int(os.environ.get("RATE_LIMIT_WRITES", "200"))
_WINDOW_S      = int(os.environ.get("RATE_LIMIT_WINDOW_S", "60"))

# Paths that bypass the write rate limiter (they have their own or are special)
_EXEMPT_PATHS = {"/api/ingest", "/api/sync/receive", "/auth/login"}
_WRITE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}


# ── Sliding window counter ────────────────────────────────────────────────────

class _SlidingWindow:
    """Per-IP hit log using a list of monotonic timestamps."""

    __slots__ = ("_hits",)

    def __init__(self) -> None:
        self._hits: List[float] = []

    def check(self, max_req: int, window_s: float) -> Tuple[bool, int]:
        """
        Record a hit and return (allowed, retry_after_seconds).
        Prunes expired entries on every call.
        """
        now = time.monotonic()
        cutoff = now - window_s
        # Prune old hits
        self._hits = [h for h in self._hits if h > cutoff]
        if len(self._hits) >= max_req:
            retry = int(self._hits[0] + window_s - now) + 1
            return False, retry
        self._hits.append(now)
        return True, 0


class RateLimitMiddleware(BaseHTTPMiddleware):
    """FastAPI/Starlette middleware applying sliding-window rate limits."""

    def __init__(self, app, max_requests: int = _MAX_REQUESTS, window_s: int = _WINDOW_S):
        super().__init__(app)
        self._max = max_requests
        self._window = window_s
        self._store: Dict[str, _SlidingWindow] = {}
        self._lock = asyncio.Lock()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in _WRITE_METHODS:
            return await call_next(request)
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        ip = (request.client.host if request.client else "unknown")

        async with self._lock:
            if ip not in self._store:
                self._store[ip] = _SlidingWindow()
            allowed, retry_after = self._store[ip].check(self._max, self._window)

        if not allowed:
            return JSONResponse(
                {
                    "error": "rate limit exceeded",
                    "detail": f"Max {self._max} write requests per {self._window}s",
                    "retry_after": retry_after,
                },
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)
