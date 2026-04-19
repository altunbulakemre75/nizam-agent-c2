"""FreeTAKServer / ATAK mesh'e CoT gönderen basit TCP yayıncısı.

Not: mTLS enrollment üretim gereksinimi — bu katman geliştirme/demo için.
Üretim: pytak + FTSProtocol + mTLS cert rotation.
"""
from __future__ import annotations

import asyncio
import logging
from xml.etree import ElementTree as ET

log = logging.getLogger(__name__)


class TAKSender:
    """TCP üzerinden CoT paketi gönderen basit yazar.

    FreeTAKServer default ports: 8087 (TCP CoT), 8089 (TLS).
    """

    def __init__(self, host: str, port: int = 8087) -> None:
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        log.info("TAK connected %s:%d", self.host, self.port)

    async def send(self, event: ET.Element) -> None:
        if self._writer is None:
            raise RuntimeError("TAKSender bağlı değil; önce connect() çağırın")
        payload = ET.tostring(event, encoding="utf-8")
        self._writer.write(payload + b"\n")
        await self._writer.drain()

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            await self._writer.wait_closed()
            self._writer = None
            self._reader = None
