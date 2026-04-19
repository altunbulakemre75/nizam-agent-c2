"""ODID (Remote ID) tespit servisi — ham ODID paketlerini NATS'e yayınlar.

Veri kaynakları (pluggable):
  - Bluetooth Legacy/LE (gelecek: bluez + D-Bus)
  - WiFi NAN / WiFi Beacon (gelecek: scapy / airmon-ng)
  - Mock source (test için, stdin'den hex satır)

Kullanım:
    python -m services.detectors.rf.odid_service \
        --sensor-id edge-01 --nats nats://localhost:6222 --source mock
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import TYPE_CHECKING, AsyncIterator

from prometheus_client import Counter, start_http_server
from shared.clock import get_clock

from services.detectors.rf.odid_parser import parse_message
from services.schemas.rf import (
    ODIDBasicID,
    ODIDEvent,
    ODIDLocation,
    ODIDMessageType,
)

if TYPE_CHECKING:
    import nats


# ── Prometheus ────────────────────────────────────────────────────
_messages_total = Counter(
    "nizam_rf_odid_messages_total",
    "Toplam ODID mesaj sayısı",
    ["sensor_id", "msg_type"],
)
_parse_errors_total = Counter(
    "nizam_rf_odid_parse_errors_total",
    "ODID ayrıştırma hataları",
    ["sensor_id"],
)


class NATSSubject:
    @staticmethod
    def odid(sensor_id: str) -> str:
        return f"nizam.raw.rf.odid.{sensor_id}"


# ── Saf fonksiyon: raw bytes → ODIDEvent ──────────────────────────

def build_odid_event(
    raw: bytes,
    sensor_id: str,
    source: str,
    rssi_dbm: float | None = None,
) -> ODIDEvent | None:
    """Tek bir 25-bayt ODID mesajını ODIDEvent'e dönüştürür.

    Bilinmeyen mesaj tipleri None döner (örn. Auth, SelfID).
    """
    try:
        msg_type, parsed = parse_message(raw)
    except ValueError:
        _parse_errors_total.labels(sensor_id=sensor_id).inc()
        return None

    _messages_total.labels(sensor_id=sensor_id, msg_type=msg_type.name).inc()

    basic_id: ODIDBasicID | None = None
    location: ODIDLocation | None = None
    if msg_type == ODIDMessageType.BASIC_ID and isinstance(parsed, ODIDBasicID):
        basic_id = parsed
    elif msg_type == ODIDMessageType.LOCATION and isinstance(parsed, ODIDLocation):
        location = parsed
    else:
        return None  # diğer tipler bu serviste yayınlanmaz

    return ODIDEvent(
        sensor_id=sensor_id,
        timestamp_iso=get_clock().utcnow_iso(),
        source=source,
        rssi_dbm=rssi_dbm,
        basic_id=basic_id,
        location=location,
    )


async def publish_event(nc: "nats.aio.client.Client", event: ODIDEvent) -> None:
    subject = NATSSubject.odid(event.sensor_id)
    await nc.publish(subject, event.model_dump_json().encode())


# ── Veri kaynakları ───────────────────────────────────────────────

async def mock_source_from_stdin() -> AsyncIterator[bytes]:
    """stdin'den hex satır oku (test/geliştirme için)."""
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            return
        hex_str = line.strip().replace(" ", "")
        if not hex_str:
            continue
        try:
            yield bytes.fromhex(hex_str)
        except ValueError:
            continue


# ── Ana servis ────────────────────────────────────────────────────

async def run(sensor_id: str, nats_url: str, source: str) -> None:
    import nats

    nc = await nats.connect(nats_url)

    if source == "mock":
        byte_stream = mock_source_from_stdin()
    else:
        raise NotImplementedError(f"Kaynak henüz desteklenmiyor: {source}")

    try:
        async for raw in byte_stream:
            event = build_odid_event(raw, sensor_id=sensor_id, source=source)
            if event is not None:
                await publish_event(nc, event)
    finally:
        await nc.drain()


def main() -> None:
    parser = argparse.ArgumentParser(description="NIZAM ODID RF Servisi")
    parser.add_argument("--sensor-id", default="rf-01")
    parser.add_argument("--nats", default="nats://localhost:6222")
    parser.add_argument("--source", default="mock", choices=["mock", "bluetooth", "wifi-nan"])
    parser.add_argument("--metrics-port", type=int, default=8002)
    args = parser.parse_args()

    start_http_server(args.metrics_port)
    asyncio.run(run(args.sensor_id, args.nats, args.source))


if __name__ == "__main__":
    main()
