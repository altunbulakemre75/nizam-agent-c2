"""
cop/circuit_breaker.py — Per-IP circuit breaker for the /ingest endpoint.

Pattern:  CLOSED → OPEN → HALF_OPEN → CLOSED

  CLOSED   : normal operation; bad requests increment an error counter.
  OPEN     : circuit is tripped; all requests from this IP are rejected with 503.
             After COOLDOWN_S the circuit moves to HALF_OPEN.
  HALF_OPEN: one probe request is let through.  Success → CLOSED; failure → OPEN.

Trip condition (per IP):
  >= FAIL_THRESHOLD bad-ingest events (4xx from validation / size / auth) within
  FAIL_WINDOW_S seconds.

Global override:
  If the total error rate across all IPs exceeds GLOBAL_TRIP_RATE errors/s
  for GLOBAL_WINDOW_S consecutive seconds the global circuit opens for
  GLOBAL_COOLDOWN_S, rejecting all ingest traffic regardless of IP.

Configuration (env vars):
  CB_FAIL_THRESHOLD      int   default 10
  CB_FAIL_WINDOW_S       float default 10
  CB_COOLDOWN_S          float default 30
  CB_GLOBAL_TRIP_RATE    float default 50  (errors/s)
  CB_GLOBAL_WINDOW_S     float default 5
  CB_GLOBAL_COOLDOWN_S   float default 15
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from enum import Enum
from typing import Deque, Dict, Optional, Tuple

# ── Configuration ─────────────────────────────────────────────────────────────

FAIL_THRESHOLD   = int(float(os.environ.get("CB_FAIL_THRESHOLD",   "10")))
FAIL_WINDOW_S    = float(os.environ.get("CB_FAIL_WINDOW_S",        "10"))
COOLDOWN_S       = float(os.environ.get("CB_COOLDOWN_S",           "30"))

GLOBAL_TRIP_RATE = float(os.environ.get("CB_GLOBAL_TRIP_RATE",     "50"))
GLOBAL_WINDOW_S  = float(os.environ.get("CB_GLOBAL_WINDOW_S",      "5"))
GLOBAL_COOLDOWN_S = float(os.environ.get("CB_GLOBAL_COOLDOWN_S",   "15"))


# ── States ────────────────────────────────────────────────────────────────────

class _State(Enum):
    CLOSED     = "closed"
    OPEN       = "open"
    HALF_OPEN  = "half_open"


# ── Per-IP circuit ─────────────────────────────────────────────────────────────

class _IPCircuit:
    """Sliding-window circuit breaker for a single IP."""

    __slots__ = ("state", "_errors", "_opened_at", "_probe_in_flight")

    def __init__(self) -> None:
        self.state           = _State.CLOSED
        self._errors: Deque[float] = deque()   # monotonic timestamps of bad events
        self._opened_at: float = 0.0
        self._probe_in_flight: bool = False

    def _prune(self) -> None:
        cutoff = time.monotonic() - FAIL_WINDOW_S
        while self._errors and self._errors[0] < cutoff:
            self._errors.popleft()

    def record_bad(self) -> None:
        """Register one bad-ingest event; may trip the circuit."""
        now = time.monotonic()
        self._errors.append(now)
        self._prune()
        if self.state == _State.CLOSED and len(self._errors) >= FAIL_THRESHOLD:
            self.state      = _State.OPEN
            self._opened_at = now

    def check(self) -> Tuple[bool, str]:
        """
        Return (allowed, reason).
        allowed=True  → request may proceed.
        allowed=False → circuit is open; include reason in 503 response.
        """
        now = time.monotonic()

        if self.state == _State.CLOSED:
            return True, ""

        if self.state == _State.OPEN:
            if now - self._opened_at >= COOLDOWN_S:
                self.state            = _State.HALF_OPEN
                self._probe_in_flight = False
                # fall through to HALF_OPEN handling
            else:
                remaining = int(COOLDOWN_S - (now - self._opened_at)) + 1
                return False, f"circuit open; retry in {remaining}s"

        # HALF_OPEN
        if not self._probe_in_flight:
            self._probe_in_flight = True
            return True, ""          # let one probe through
        return False, "circuit half-open; probe in flight"

    def record_success(self) -> None:
        """Call after a successful ingest while in HALF_OPEN → closes circuit."""
        if self.state == _State.HALF_OPEN:
            self.state            = _State.CLOSED
            self._errors.clear()
            self._probe_in_flight = False

    def record_failure_probe(self) -> None:
        """Call after a failed probe in HALF_OPEN → reopens circuit."""
        if self.state == _State.HALF_OPEN:
            self.state            = _State.OPEN
            self._opened_at       = time.monotonic()
            self._probe_in_flight = False


# ── Global circuit ─────────────────────────────────────────────────────────────

class _GlobalCircuit:
    """Aggregate rate-based circuit breaker.  Trips when errors/s > threshold."""

    def __init__(self) -> None:
        self._errors: Deque[float] = deque()
        self._state                = _State.CLOSED
        self._opened_at: float     = 0.0
        self._lock                 = threading.Lock()

    def record_bad(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._errors.append(now)
            # Prune window
            cutoff = now - GLOBAL_WINDOW_S
            while self._errors and self._errors[0] < cutoff:
                self._errors.popleft()
            # Trip?
            rate = len(self._errors) / GLOBAL_WINDOW_S
            if self._state == _State.CLOSED and rate >= GLOBAL_TRIP_RATE:
                self._state     = _State.OPEN
                self._opened_at = now

    def check(self) -> Tuple[bool, str]:
        with self._lock:
            if self._state == _State.CLOSED:
                return True, ""
            now = time.monotonic()
            if now - self._opened_at >= GLOBAL_COOLDOWN_S:
                self._state = _State.CLOSED
                self._errors.clear()
                return True, ""
            remaining = int(GLOBAL_COOLDOWN_S - (now - self._opened_at)) + 1
            return False, f"global circuit open (flood); retry in {remaining}s"


# ── Registry ──────────────────────────────────────────────────────────────────

_lock     = threading.Lock()
_circuits: Dict[str, _IPCircuit] = {}
_global   = _GlobalCircuit()


def _get_or_create(ip: str) -> _IPCircuit:
    with _lock:
        if ip not in _circuits:
            _circuits[ip] = _IPCircuit()
        return _circuits[ip]


# ── Public API ────────────────────────────────────────────────────────────────

def check(ip: str) -> Tuple[bool, str]:
    """
    Call at the top of /ingest before doing any work.
    Returns (allowed, reason).  If allowed=False, return HTTP 503.
    """
    # Global check first
    ok, reason = _global.check()
    if not ok:
        return False, reason
    # Per-IP check
    circuit = _get_or_create(ip)
    with _lock:
        return circuit.check()


def record_bad(ip: str) -> None:
    """Call whenever /ingest rejects a request (4xx) due to bad input."""
    _global.record_bad()
    circuit = _get_or_create(ip)
    with _lock:
        circuit.record_bad()
        # If this was a failed probe → re-open
        circuit.record_failure_probe()


def record_success(ip: str) -> None:
    """Call after a request is successfully processed (may close HALF_OPEN)."""
    circuit = _get_or_create(ip)
    with _lock:
        circuit.record_success()


def state_for(ip: str) -> str:
    """Return the current circuit state string for an IP (used in /api/metrics)."""
    with _lock:
        c = _circuits.get(ip)
        return c.state.value if c else "closed"


def stats() -> dict:
    """Aggregate stats for /api/metrics."""
    with _lock:
        open_count      = sum(1 for c in _circuits.values() if c.state == _State.OPEN)
        half_open_count = sum(1 for c in _circuits.values() if c.state == _State.HALF_OPEN)
        total_ips       = len(_circuits)
    global_ok, _ = _global.check()
    return {
        "global_open":      not global_ok,
        "total_ips_tracked": total_ips,
        "open_circuits":    open_count,
        "half_open_circuits": half_open_count,
        "config": {
            "fail_threshold":   FAIL_THRESHOLD,
            "fail_window_s":    FAIL_WINDOW_S,
            "cooldown_s":       COOLDOWN_S,
            "global_trip_rate": GLOBAL_TRIP_RATE,
            "global_window_s":  GLOBAL_WINDOW_S,
            "global_cooldown_s": GLOBAL_COOLDOWN_S,
        },
    }


def reset() -> None:
    """Clear all state (tests / /api/reset)."""
    global _global
    with _lock:
        _circuits.clear()
    _global = _GlobalCircuit()
