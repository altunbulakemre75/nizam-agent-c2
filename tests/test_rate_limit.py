"""Rate limit + circuit breaker tests — DoS savunması."""
from __future__ import annotations

import asyncio
import time

import pytest

from shared.rate_limit import QueueCircuitBreaker, SlidingWindowLimiter


@pytest.mark.asyncio
async def test_sliding_window_allows_under_limit():
    limiter = SlidingWindowLimiter(max_events_per_sec=5, window_s=1.0)
    for _ in range(5):
        assert await limiter.allow("cam-01") is True


@pytest.mark.asyncio
async def test_sliding_window_blocks_over_limit():
    limiter = SlidingWindowLimiter(max_events_per_sec=5, window_s=1.0)
    for _ in range(5):
        await limiter.allow("cam-01")
    # 6. event drop edilmeli
    assert await limiter.allow("cam-01") is False


@pytest.mark.asyncio
async def test_sliding_window_resets_after_window():
    limiter = SlidingWindowLimiter(max_events_per_sec=2, window_s=0.1)
    assert await limiter.allow("x") is True
    assert await limiter.allow("x") is True
    assert await limiter.allow("x") is False
    await asyncio.sleep(0.15)
    assert await limiter.allow("x") is True


@pytest.mark.asyncio
async def test_sliding_window_per_sensor_isolated():
    """Bir sensörün flood'u diğerini etkilememeli."""
    limiter = SlidingWindowLimiter(max_events_per_sec=2, window_s=1.0)
    await limiter.allow("noisy")
    await limiter.allow("noisy")
    # noisy dolsa da quiet hâlâ geçebilmeli
    assert await limiter.allow("quiet") is True


# ── Circuit breaker ───────────────────────────────────────────────

def test_breaker_allows_below_soft_threshold():
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    breaker = QueueCircuitBreaker(q, "test", soft_threshold=0.5, hard_threshold=0.9)
    # Queue 0 → her şey geçer
    assert breaker.allow("cam-01") is True


def test_breaker_drops_non_critical_at_soft():
    q: asyncio.Queue = asyncio.Queue(maxsize=10)
    for _ in range(6):   # %60 dolu
        q.put_nowait("x")
    breaker = QueueCircuitBreaker(q, "test", soft_threshold=0.5, hard_threshold=0.9)
    assert breaker.allow("cam-01", is_critical=False) is False


def test_breaker_accepts_critical_at_soft():
    q: asyncio.Queue = asyncio.Queue(maxsize=10)
    for _ in range(6):   # %60 dolu
        q.put_nowait("x")
    breaker = QueueCircuitBreaker(q, "test", soft_threshold=0.5, hard_threshold=0.9)
    # Critical sensor (örn. ODID) hard'ı aşmamışsa geçer
    assert breaker.allow("rf-01", is_critical=True) is True


def test_breaker_drops_all_at_hard():
    q: asyncio.Queue = asyncio.Queue(maxsize=10)
    for _ in range(10):   # %100
        q.put_nowait("x")
    breaker = QueueCircuitBreaker(q, "test", soft_threshold=0.5, hard_threshold=0.9)
    assert breaker.allow("rf-01", is_critical=True) is False   # critical bile drop
