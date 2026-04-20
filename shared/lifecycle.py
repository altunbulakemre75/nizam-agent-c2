"""Servis lifecycle yardımcıları — SIGTERM/SIGINT'te graceful shutdown.

Kullanım:
    async def my_worker(shutdown: asyncio.Event):
        while not shutdown.is_set():
            await do_work()
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

    asyncio.run(run_with_shutdown(my_worker))
"""
from __future__ import annotations

import asyncio
import logging
import signal
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


def install_shutdown_handlers(shutdown: asyncio.Event) -> None:
    """SIGTERM ve SIGINT'te shutdown event'ini set et."""
    loop = asyncio.get_event_loop()

    def _handler(sig: int) -> None:
        log.info("Shutdown signal alındı: %s", signal.Signals(sig).name)
        shutdown.set()

    # Windows SIGTERM desteklemez — sadece SIGINT (Ctrl+C)
    signals = [signal.SIGINT]
    if hasattr(signal, "SIGTERM"):
        signals.append(signal.SIGTERM)

    for sig in signals:
        try:
            loop.add_signal_handler(sig, _handler, sig)
        except NotImplementedError:
            # Windows loop.add_signal_handler desteklemez
            signal.signal(sig, lambda s, _f: _handler(s))


async def run_with_shutdown(
    worker: Callable[[asyncio.Event], Awaitable[None]],
    timeout_s: float = 10.0,
) -> None:
    """Worker'ı shutdown event ile çalıştır, SIGTERM geldiğinde temiz kapan."""
    shutdown = asyncio.Event()
    install_shutdown_handlers(shutdown)

    task = asyncio.create_task(worker(shutdown))
    try:
        await task
    except asyncio.CancelledError:
        log.info("Worker iptal edildi")
    finally:
        if not task.done():
            shutdown.set()
            try:
                await asyncio.wait_for(task, timeout=timeout_s)
            except asyncio.TimeoutError:
                log.warning("Worker %ss içinde kapanmadı, task cancel", timeout_s)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
