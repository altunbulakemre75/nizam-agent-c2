"""
ai/registry.py  —  Analyzer plugin registry

The tactical engine in cop/server.py currently calls 7 analysis modules
with hardcoded closures. Adding an 8th module today means editing
`_ai_run_tactical_compute`, adding an import at the top of server.py,
wiring the result into the broadcast payload, and remembering to clear
its state in `/api/reset`. That's four places to touch in three files
just to ship a rule.

This module is the opt-in alternative: any new analysis module declares
an `Analyzer`, registers it, and the tactical engine will pick it up
without server.py changes. Existing modules stay as they are — this is
purely additive.

Usage
-----
    from ai.registry import Analyzer, register, run_all

    @register
    def swarm_cluster_density(ctx: "TacticalContext"):
        return Analyzer(
            name="swarm_cluster_density",
            stage="tactical.analyze",
            fn=lambda: _compute(ctx),
            result_key="cluster_density",
        )

Tests
-----
A contract test in `tests/test_registry.py` locks the shape of the
registry so future refactors can't accidentally break extensibility.

What this module deliberately does NOT do
------------------------------------------
  - It does not rewire the existing tactical sub-modules (that is a
    bigger refactor and would risk regressions in the core hot path).
  - It does not try to be a general DAG scheduler — only a flat phase
    list for now. Dependency graphs come later if needed.
  - It does not execute on its own thread pool. The caller decides how
    to run the registered analyzers (serial or via ThreadPoolExecutor).

This is the minimum surface that lets new modules stop editing server.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from shared.clock import get_clock


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class TacticalContext:
    """The snapshot an analyzer sees. Mirrors what `_ai_run_tactical_compute`
    already passes around, but as a single object rather than eight kwargs.
    """
    tracks:  Dict[str, Dict]
    threats: Dict[str, Dict]
    assets:  Dict[str, Dict]
    zones:   Dict[str, Dict]
    extras:  Dict[str, Any] = field(default_factory=dict)


@dataclass
class Analyzer:
    """Single plugin descriptor.

    fn          : zero-argument callable that receives the context via closure
                  and returns any serialisable result
    result_key  : key under which the result is stored in the registry's
                  output dict (defaults to `name`)
    stage       : free-form label for debugging — "tactical.analyze",
                  "post.fire", "periodic", etc.
    """
    name:       str
    fn:         Callable[[], Any]
    result_key: Optional[str] = None
    stage:      str = "tactical.analyze"


# ── Registry state ───────────────────────────────────────────────────────────

_registry: List[Analyzer] = []
_current_context: Optional[TacticalContext] = None


def set_context(ctx: TacticalContext) -> None:
    """Inject the current tactical snapshot so plugin fn closures can read it.

    Called once per tactical cycle by the engine before run_all(). Plugins
    that need per-cycle track/threat data call get_context() inside their fn.
    """
    global _current_context
    _current_context = ctx


def get_context() -> Optional[TacticalContext]:
    """Return the context injected by the most recent tactical cycle, or None."""
    return _current_context


def register(analyzer: Analyzer) -> Analyzer:
    """Add an analyzer to the process-wide registry. Idempotent by name."""
    # Replace if already registered (keeps hot-reload friendly)
    for i, existing in enumerate(_registry):
        if existing.name == analyzer.name:
            _registry[i] = analyzer
            return analyzer
    _registry.append(analyzer)
    return analyzer


def unregister(name: str) -> bool:
    """Remove an analyzer from the registry. Returns True if removed."""
    for i, a in enumerate(_registry):
        if a.name == name:
            del _registry[i]
            return True
    return False


def list_analyzers() -> List[Analyzer]:
    """Return a shallow copy of the current registry (for debugging/tests)."""
    return list(_registry)


def clear() -> None:
    """Wipe the registry. Used by tests; do not call from production code."""
    _registry.clear()


# ── Runner ───────────────────────────────────────────────────────────────────

def run_all(stage: str = "tactical.analyze") -> Dict[str, Any]:
    """Execute every registered analyzer for the given stage, in declaration order.

    Returns a dict keyed by `result_key` (or `name` if not set), with values
    `{"result": ..., "elapsed_ms": ..., "stage": ...}`. Failures are caught
    and surfaced as `{"error": "..."}` so one bad plugin can't take down the
    whole loop.
    """
    clock = get_clock()
    out: Dict[str, Any] = {}
    for a in _registry:
        if a.stage != stage:
            continue
        t0 = clock.monotonic()
        try:
            result = a.fn()
            out[a.result_key or a.name] = {
                "result":     result,
                "elapsed_ms": round((clock.monotonic() - t0) * 1000, 2),
                "stage":      a.stage,
            }
        except Exception as exc:
            out[a.result_key or a.name] = {
                "error":      f"{type(exc).__name__}: {exc}",
                "elapsed_ms": round((clock.monotonic() - t0) * 1000, 2),
                "stage":      a.stage,
            }
    return out


__all__ = [
    "Analyzer", "TacticalContext",
    "register", "unregister", "list_analyzers", "clear",
    "run_all",
    "set_context", "get_context",
]
