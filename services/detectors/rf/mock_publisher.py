"""RF mock publisher — gerçek SDR donanımı yokken ODID simüle eder.

Saha demosu için: kamera gerçek drone tespit ederken bu servis
sahte ODID mesajları yayınlar → fusion iki kaynağı birleştirir →
"multi-sensor" demo kanıtı (donanım bekleyen Faz 2 için köprü).

Kullanım:
    python -m services.detectors.rf.mock_publisher \
        --sensor-id rf-sim-01 --nats nats://localhost:6222 --rate 2.0
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

from prometheus_client import Counter, start_http_server
from shared.clock import get_clock

from services.detectors.rf.odid_service import NATSSubject, publish_event
from services.schemas.rf import (
    ODIDBasicID,
    ODIDEvent,
    ODIDIDType,
    ODIDLocation,
    ODIDUAType,
)

if TYPE_CHECKING:
    import nats

log = logging.getLogger(__name__)

_mock_sent = Counter("nizam_rf_mock_sent_total", "Mock ODID yayın sayısı", ["sensor_id"])


@dataclass
class MockDrone:
    """Sahte drone — sabit yörünge üzerinde hareket eder."""
    uas_id: str
    manufacturer: str       # "DJI", "Parrot", ...
    ua_type: ODIDUAType
    # Yörünge (Ankara merkezinde dairesel uçuş)
    center_lat: float = 39.9334
    center_lon: float = 32.8597
    radius_m: float = 500.0
    altitude_m: float = 120.0
    speed_mps: float = 12.0
    phase_deg: float = 0.0   # başlangıç fazı
    start_time: float = 0.0


def _drone_position(drone: MockDrone, elapsed_s: float) -> tuple[float, float, float, float]:
    """Drone'un şu anki (lat, lon, heading, speed) değerleri."""
    EARTH_R = 6378137.0
    angular_speed = drone.speed_mps / drone.radius_m  # rad/s
    angle = math.radians(drone.phase_deg) + angular_speed * elapsed_s

    d_lat = math.degrees((drone.radius_m * math.sin(angle)) / EARTH_R)
    d_lon = math.degrees(
        (drone.radius_m * math.cos(angle))
        / (EARTH_R * math.cos(math.radians(drone.center_lat)))
    )
    lat = drone.center_lat + d_lat
    lon = drone.center_lon + d_lon
    heading = (math.degrees(angle) + 90) % 360
    return lat, lon, heading, drone.speed_mps


async def run(sensor_id: str, nats_url: str, rate_hz: float, drone_count: int) -> None:
    import nats

    nc = await nats.connect(nats_url)
    clock = get_clock()
    start_ts = clock.monotonic()

    # N tane sahte drone
    drones = [
        MockDrone(
            uas_id=f"MOCK-{random.choice(['DJI', 'PARROT', 'AUTEL'])}-{1000 + i}",
            manufacturer="MOCK",
            ua_type=ODIDUAType.HELICOPTER_MULTIROTOR,
            center_lat=39.9334 + (i - drone_count / 2) * 0.002,
            center_lon=32.8597,
            radius_m=300 + i * 100,
            speed_mps=8.0 + i * 2,
            phase_deg=(i * 360.0 / max(drone_count, 1)),
        )
        for i in range(drone_count)
    ]

    log.info("Mock RF publisher: %d drone, %.1fHz, sensor=%s", drone_count, rate_hz, sensor_id)

    interval = 1.0 / rate_hz
    try:
        while True:
            elapsed = clock.monotonic() - start_ts
            for drone in drones:
                lat, lon, heading, speed = _drone_position(drone, elapsed)
                event = ODIDEvent(
                    sensor_id=sensor_id,
                    timestamp_iso=clock.utcnow_iso(),
                    source="mock-rf",
                    rssi_dbm=-60.0 - random.uniform(0, 20),  # -60 to -80 dBm
                    basic_id=ODIDBasicID(
                        id_type=ODIDIDType.SERIAL_NUMBER,
                        ua_type=drone.ua_type,
                        uas_id=drone.uas_id,
                    ),
                    location=ODIDLocation(
                        latitude=lat, longitude=lon,
                        altitude_geo_m=drone.altitude_m,
                        heading_deg=heading,
                        speed_horizontal_mps=speed,
                    ),
                )
                await publish_event(nc, event)
                _mock_sent.labels(sensor_id=sensor_id).inc()
            await asyncio.sleep(interval)
    finally:
        await nc.drain()


def main() -> None:
    parser = argparse.ArgumentParser(description="NIZAM RF Mock Publisher")
    parser.add_argument("--sensor-id", default="rf-mock-01")
    parser.add_argument("--nats", default="nats://localhost:6222")
    parser.add_argument("--rate", type=float, default=2.0, help="Yayın frekansı (Hz)")
    parser.add_argument("--drones", type=int, default=3, help="Sahte drone sayısı")
    parser.add_argument("--metrics-port", type=int, default=8007)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    start_http_server(args.metrics_port)
    asyncio.run(run(args.sensor_id, args.nats, args.rate, args.drones))


if __name__ == "__main__":
    main()
