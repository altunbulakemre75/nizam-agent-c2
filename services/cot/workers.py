"""pytak QueueWorker deseni — NATS → CoT → TAK Server.

Plan'daki adsbxcot şablonunun NIZAM uyarlaması. İki worker:
  - FusionTrackWorker: nizam.tracks.active subject'ini dinler, CoT üretir
  - OdidWorker: nizam.raw.rf.odid.* subject'ini dinler, direkt CoT üretir

pytak TX queue'sina CoT yayınlar; pytak asenkron olarak FreeTAKServer'a
TCP/TLS ile gönderir.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from services.cot.cot_builder import serialize
from services.cot.enrichment import enrich_event
from services.cot.fusion_to_cot import track_to_cot
from services.cot.odid_to_cot import odid_event_to_cot

if TYPE_CHECKING:
    import pytak

log = logging.getLogger(__name__)


class FusionTrackWorker:
    """NATS nizam.tracks.active → pytak TX queue.

    pytak kurulu değilse (test/dev) kendi basit TX kuyruğunu kullanır.
    """

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

    async def run(self) -> None:
        import nats

        self._nc = await nats.connect(self.nats_url)

        async def handler(msg: "nats.aio.msg.Msg") -> None:
            try:
                track = json.loads(msg.data.decode())
            except json.JSONDecodeError:
                return
            event = track_to_cot(track, ref_lat=self.ref_lat, ref_lon=self.ref_lon)
            if self.enrichment_enabled:
                enrich_event(event)
            await self.tx_queue.put(serialize(event))

        await self._nc.subscribe("nizam.tracks.active", cb=handler)
        log.info("FusionTrackWorker bağlandı: %s", self.nats_url)

        while True:
            await asyncio.sleep(3600)


class OdidWorker:
    """NATS nizam.raw.rf.odid.* → pytak TX queue."""

    def __init__(self, nats_url: str, tx_queue: asyncio.Queue) -> None:
        self.nats_url = nats_url
        self.tx_queue = tx_queue

    async def run(self) -> None:
        import nats

        nc = await nats.connect(self.nats_url)

        async def handler(msg: "nats.aio.msg.Msg") -> None:
            try:
                event = json.loads(msg.data.decode())
            except json.JSONDecodeError:
                return
            cot = odid_event_to_cot(event)
            if cot is None:
                return
            enrich_event(cot)
            await self.tx_queue.put(serialize(cot))

        await nc.subscribe("nizam.raw.rf.odid.>", cb=handler)
        log.info("OdidWorker bağlandı: %s", self.nats_url)
        while True:
            await asyncio.sleep(3600)


class SimpleTXSender:
    """pytak kurulu değilse basit TCP yazar."""

    def __init__(self, tx_queue: asyncio.Queue, host: str, port: int = 8087) -> None:
        self.tx_queue = tx_queue
        self.host = host
        self.port = port

    async def run(self) -> None:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        _ = reader
        log.info("TAK sender bağlandı: %s:%d", self.host, self.port)
        try:
            while True:
                payload: bytes = await self.tx_queue.get()
                writer.write(payload + b"\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


async def run_pytak_pipeline(
    nats_url: str, tak_host: str, tak_port: int,
    ref_lat: float, ref_lon: float,
) -> None:
    """Tüm worker'ları paralel çalıştır."""
    tx_queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
    workers = [
        FusionTrackWorker(nats_url, tx_queue, ref_lat, ref_lon).run(),
        OdidWorker(nats_url, tx_queue).run(),
        SimpleTXSender(tx_queue, tak_host, tak_port).run(),
    ]
    await asyncio.gather(*workers)
