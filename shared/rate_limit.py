"""Per-sensor rate limit + queue-depth circuit breaker.

DoS savunması: bir sensör saniyede 10k mesaj gönderse bile fusion
kuyruğunu şişirmez. İki katman:
  1. SlidingWindowLimiter — per-sensor max events/sec
  2. CircuitBreaker — aşağı-akış kuyruk > threshold ise drop

Her iki yol da idempotent, thread-safe (asyncio lock) ve Prometheus
metrikleri yayınlar.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque

from prometheus_client import Counter, Gauge

_rate_dropped = Counter(
    "nizam_rate_limit_dropped_total",
    "Rate limit veya circuit breaker tarafından atılan mesaj sayısı",
    ["sensor_id", "reason"],
)
_queue_depth = Gauge(
    "nizam_queue_depth_ratio",
    "Aşağı-akış kuyruk doluluğu (0..1)",
    ["component"],
)


class SlidingWindowLimiter:
    """Her sensor_id için son N saniyedeki event sayısı >= max_events ise drop."""

    def __init__(self, max_events_per_sec: int = 100, window_s: float = 1.0) -> None:
        self.max_events = max_events_per_sec
        self.window_s = window_s
        self._timestamps: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def allow(self, sensor_id: str) -> bool:
        """True → kabul, False → drop (metric counter artar)."""
        now = time.monotonic()
        cutoff = now - self.window_s
        async with self._lock:
            times = self._timestamps.setdefault(sensor_id, deque())
            while times and times[0] < cutoff:
                times.popleft()
            if len(times) >= self.max_events:
                _rate_dropped.labels(sensor_id=sensor_id, reason="rate_limit").inc()
                return False
            times.append(now)
            return True

    def current_rate(self, sensor_id: str) -> int:
        return len(self._timestamps.get(sensor_id, deque()))


class QueueCircuitBreaker:
    """Kuyruk belirli bir doluluğu aşınca low-priority drop.

    threshold=0.8 → kuyruk %80 doluysa drop başlar, %95'te acil mode.
    Her sensör için aynı kural; sensor_priority daha sonra eklenir.
    """

    def __init__(
        self, queue: asyncio.Queue, component_name: str,
        soft_threshold: float = 0.80, hard_threshold: float = 0.95,
    ) -> None:
        self.queue = queue
        self.component = component_name
        self.soft = soft_threshold
        self.hard = hard_threshold

    def _depth_ratio(self) -> float:
        maxsize = self.queue.maxsize or 1
        ratio = self.queue.qsize() / maxsize
        _queue_depth.labels(component=self.component).set(ratio)
        return ratio

    def allow(self, sensor_id: str, is_critical: bool = False) -> bool:
        """Track is_critical=True ise hard threshold'u dener, değilse soft."""
        ratio = self._depth_ratio()
        if ratio >= self.hard:
            _rate_dropped.labels(sensor_id=sensor_id, reason="hard_breaker").inc()
            return False
        if ratio >= self.soft and not is_critical:
            _rate_dropped.labels(sensor_id=sensor_id, reason="soft_breaker").inc()
            return False
        return True
