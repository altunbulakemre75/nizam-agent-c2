"""WiFi OUI tespiti — bilinen drone MAC prefix'lerini probe request'lerinden tanı.

Bu servis iki modda çalışır:
  - live    : scapy ile monitor-mode 802.11 capture (Linux + monitor-mode iface)
  - mock    : stdin'den MAC adresi satırları oku (test/demo)

Donanım gereksinimi: monitor-mode destekli WiFi adapter (örn. Alfa AWUS036ACS).
Üretim için Linux önerilir; Windows'ta monitor mode sınırlı.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator

from prometheus_client import Counter, start_http_server
from shared.clock import get_clock

from services.schemas.rf import WiFiOUIEvent

if TYPE_CHECKING:
    import nats

log = logging.getLogger(__name__)

OUI_PATH = Path(__file__).parent / "drone_ouis.json"

_wifi_events_total = Counter(
    "nizam_rf_wifi_events_total",
    "WiFi OUI eşleşmesi sayısı",
    ["sensor_id", "vendor"],
)


def load_oui_table(path: Path | None = None) -> dict[str, str]:
    """OUI → vendor eşleştirme tablosunu yükle."""
    p = path or OUI_PATH
    return json.loads(p.read_text(encoding="utf-8"))


def mac_to_oui(mac: str) -> str:
    """İlk 3 oktet (OUI prefix). 'aa:bb:cc:dd:ee:ff' → 'AA:BB:CC'."""
    parts = mac.upper().replace("-", ":").split(":")
    if len(parts) < 3:
        raise ValueError(f"Geçersiz MAC: {mac}")
    return ":".join(parts[:3])


def match_drone(mac: str, oui_table: dict[str, str]) -> str | None:
    """MAC'ın drone üreticisine ait olup olmadığını döndür."""
    try:
        oui = mac_to_oui(mac)
    except ValueError:
        return None
    return oui_table.get(oui)


def build_wifi_event(
    mac: str, vendor: str, sensor_id: str,
    ssid: str | None = None, rssi_dbm: float | None = None,
    channel: int | None = None,
) -> WiFiOUIEvent:
    return WiFiOUIEvent(
        sensor_id=sensor_id,
        timestamp_iso=get_clock().utcnow_iso(),
        mac=mac.upper(),
        oui=mac_to_oui(mac),
        vendor=vendor,
        ssid=ssid,
        rssi_dbm=rssi_dbm,
        channel=channel,
    )


class NATSSubject:
    @staticmethod
    def wifi(sensor_id: str) -> str:
        return f"nizam.raw.rf.wifi.{sensor_id}"


async def publish_event(nc: "nats.aio.client.Client", event: WiFiOUIEvent) -> None:
    await nc.publish(NATSSubject.wifi(event.sensor_id), event.model_dump_json().encode())


async def mock_source_from_stdin() -> AsyncIterator[str]:
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            return
        mac = line.strip()
        if mac:
            yield mac


async def run(sensor_id: str, nats_url: str, source: str) -> None:
    import nats

    oui_table = load_oui_table()
    nc = await nats.connect(nats_url)

    if source != "mock":
        raise NotImplementedError(f"Kaynak desteklenmiyor: {source} (live scapy henüz yazılmadı)")

    try:
        async for mac in mock_source_from_stdin():
            vendor = match_drone(mac, oui_table)
            if vendor is None:
                continue
            event = build_wifi_event(mac, vendor, sensor_id=sensor_id)
            await publish_event(nc, event)
            _wifi_events_total.labels(sensor_id=sensor_id, vendor=vendor).inc()
    finally:
        await nc.drain()


def main() -> None:
    parser = argparse.ArgumentParser(description="NIZAM WiFi OUI Drone Detector")
    parser.add_argument("--sensor-id", default="wifi-01")
    parser.add_argument("--nats", default="nats://localhost:6222")
    parser.add_argument("--source", default="mock", choices=["mock", "live"])
    parser.add_argument("--metrics-port", type=int, default=8004)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    start_http_server(args.metrics_port)
    asyncio.run(run(args.sensor_id, args.nats, args.source))


if __name__ == "__main__":
    main()
