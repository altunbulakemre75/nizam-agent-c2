"""
shared/heartbeat.py  —  lightweight orchestrator heartbeat client

Registers an agent with the orchestrator and sends periodic heartbeats
in a background daemon thread.  Zero external dependencies (stdlib only).

Usage:
    from shared.heartbeat import Heartbeat

    hb = Heartbeat(
        name="cop-publisher",
        orchestrator_url="http://127.0.0.1:8200",
        capabilities=["ingest", "cop.track", "cop.threat"],
    )
    hb.start()          # starts background thread
    # ... do work ...
    hb.stop()           # call on shutdown (optional, daemon thread exits anyway)
    hb.report(events_sent=123)   # update metrics shown in orchestrator
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


class Heartbeat:
    def __init__(
        self,
        name: str,
        orchestrator_url: str = "http://127.0.0.1:8200",
        capabilities: Optional[List[str]] = None,
        interval_s: float = 5.0,
        timeout_s: float = 2.0,
    ) -> None:
        self.name = name
        self.base_url = orchestrator_url.rstrip("/")
        self.capabilities = capabilities or []
        self.interval_s = interval_s
        self.timeout_s = timeout_s
        self._metrics: Dict[str, Any] = {}
        self._metrics_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Register once, then start background heartbeat thread."""
        self._register()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name=f"heartbeat-{self.name}",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def report(self, **kwargs: Any) -> None:
        """Update metrics to be included in next heartbeat."""
        with self._metrics_lock:
            self._metrics.update(kwargs)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _post(self, path: str, body: Dict[str, Any]) -> bool:
        data = json.dumps(body, ensure_ascii=False).encode()
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s):
                return True
        except Exception:
            return False

    def _register(self) -> None:
        ok = self._post("/agents/register", {
            "name": self.name,
            "url": "",
            "capabilities": self.capabilities,
        })
        if not ok:
            # orchestrator may not be up yet; heartbeat loop will retry
            pass

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            with self._metrics_lock:
                metrics = dict(self._metrics)
            self._post("/agents/heartbeat", {
                "name": self.name,
                "status": "ALIVE",
                "metrics": metrics,
            })
            self._stop_event.wait(self.interval_s)
