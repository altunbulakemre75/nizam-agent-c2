"""pytak QueueWorker deseni — NATS → CoT → TAK Server.

Plan'daki adsbxcot şablonunun NIZAM uyarlaması. İki worker:
  - FusionTrackWorker: nizam.tracks.active subject'ini dinler, CoT üretir
  - OdidWorker: nizam.raw.rf.odid.* subject'ini dinler, direkt CoT üretir

Graceful shutdown: her run(shutdown_event) asyncio.Event alır.
SIGTERM'de shutdown.set() çağrılır, queue drain edilip TCP kapatılır.
"""
from __future__ import annotations

import asyncio
import json
import logging

from services.cot.cot_builder import serialize
from services.cot.enrichment import enrich_event
from services.cot.fusion_to_cot import track_to_cot
from services.cot.odid_to_cot import odid_event_to_cot

log = logging.getLogger(__name__)


class FusionTrackWorker:
    """NATS nizam.tracks.active → CoT TX queue."""

    def __init__(
        self, nats_url: str, tx_queue: asyncio.Queue,
        ref_lat: float, ref_lon: float,
        enrichment_enabled: bool = True,
    ) -> None:
        self.nats_url = nats_url
        self.tx_queue = tx_queue
        self.ref_lat = ref_lat
        self.ref_lon = ref_lon
        self.enrichment_enabled = enrichment_enabled
        self._nc = None

    async def run(self, shutdown: asyncio.Event) -> None:
        import nats

        self._nc = await nats.connect(self.nats_url)

        async def handler(msg) -> None:
            try:
                track = json.loads(msg.data.decode())
            except json.JSONDecodeError:
                return
            event = track_to_cot(track, ref_lat=self.ref_lat, ref_lon=self.ref_lon)
            if self.enrichment_enabled:
                enrich_event(event)
            await self.tx_queue.put(serialize(event))

        sub = await self._nc.subscribe("nizam.tracks.active", cb=handler)
        log.info("FusionTrackWorker bağlandı: %s", self.nats_url)

        try:
            await shutdown.wait()
        finally:
            await sub.unsubscribe()
            await self._nc.drain()


class OdidWorker:
    """NATS nizam.raw.rf.odid.* → CoT TX queue."""

    def __init__(self, nats_url: str, tx_queue: asyncio.Queue) -> None:
        self.nats_url = nats_url
        self.tx_queue = tx_queue

    async def run(self, shutdown: asyncio.Event) -> None:
        import nats

        nc = await nats.connect(self.nats_url)

        async def handler(msg) -> None:
            try:
                event = json.loads(msg.data.decode())
            except json.JSONDecodeError:
                return
            cot = odid_event_to_cot(event)
            if cot is None:
                return
            enrich_event(cot)
            await self.tx_queue.put(serialize(cot))

        sub = await nc.subscribe("nizam.raw.rf.odid.>", cb=handler)
        log.info("OdidWorker bağlandı: %s", self.nats_url)
        try:
            await shutdown.wait()
        finally:
            await sub.unsubscribe()
            await nc.drain()


class SimpleTXSender:
    """pytak kurulu değilse basit TCP yazar — graceful shutdown destekler."""

    def __init__(self, tx_queue: asyncio.Queue, host: str, port: int = 8087) -> None:
        self.tx_queue = tx_queue
        self.host = host
        self.port = port

    async def run(self, shutdown: asyncio.Event) -> None:
        _, writer = await asyncio.open_connection(self.host, self.port)
        log.info("TAK sender bağlandı: %s:%d", self.host, self.port)
        try:
            while not shutdown.is_set():
                try:
                    payload = await asyncio.wait_for(self.tx_queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                writer.write(payload + b"\n")
                await writer.drain()
                self.tx_queue.task_done()

            # Shutdown — kalan queue'yu flush etmeye çalış
            while not self.tx_queue.empty():
                try:
                    payload = self.tx_queue.get_nowait()
                    writer.write(payload + b"\n")
                    self.tx_queue.task_done()
                except asyncio.QueueEmpty:
                    break
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def run_pytak_pipeline(
    nats_url: str, tak_host: str, tak_port: int,
    ref_lat: float, ref_lon: float,
    shutdown: asyncio.Event | None = None,
) -> None:
    """Tüm worker'ları paralel çalıştır — shutdown event'ine duyarlı."""
    shutdown = shutdown or asyncio.Event()
    tx_queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)

    ft_worker = FusionTrackWorker(nats_url, tx_queue, ref_lat, ref_lon)
    odid_worker = OdidWorker(nats_url, tx_queue)
    sender = SimpleTXSender(tx_queue, tak_host, tak_port)

    tasks = [
        asyncio.create_task(ft_worker.run(shutdown)),
        asyncio.create_task(odid_worker.run(shutdown)),
        asyncio.create_task(sender.run(shutdown)),
    ]
    await asyncio.gather(*tasks, return_exceptions=True)
