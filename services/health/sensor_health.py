"""Sensor health monitor — her sensörden son mesaj zamanını izle.

NATS'te raw.* subject'lerini pasif olarak dinler, sensör_id'ye göre
son-görüldü timestamp tutar. Prometheus:
  - nizam_sensor_last_seen_timestamp{sensor_id,type}
  - nizam_sensor_offline{sensor_id} (1=offline, 0=online)

Operatör UI bu endpoint'i polling ile "cam-01 offline" gibi rozet gösterir.
30s timeout → offline state.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from dataclasses import dataclass

from prometheus_client import Gauge, start_http_server

log = logging.getLogger(__name__)

OFFLINE_TIMEOUT_S = 30.0

_last_seen = Gauge(
    "nizam_sensor_last_seen_timestamp",
    "Son mesaj unix timestamp",
    ["sensor_id", "sensor_type"],
)
_offline = Gauge(
    "nizam_sensor_offline",
    "1=offline (>30s sessiz), 0=online",
    ["sensor_id", "sensor_type"],
)


@dataclass
class SensorRecord:
    sensor_id: str
    sensor_type: str
    last_seen: float


class HealthMonitor:
    def __init__(self, offline_timeout_s: float = OFFLINE_TIMEOUT_S) -> None:
        self._records: dict[str, SensorRecord] = {}
        self._timeout = offline_timeout_s
        self._lock = asyncio.Lock()

    async def on_message(self, sensor_id: str, sensor_type: str) -> None:
        async with self._lock:
            now = time.time()
            self._records[sensor_id] = SensorRecord(sensor_id, sensor_type, now)
            _last_seen.labels(sensor_id=sensor_id, sensor_type=sensor_type).set(now)
            _offline.labels(sensor_id=sensor_id, sensor_type=sensor_type).set(0)

    async def check_offline(self) -> list[SensorRecord]:
        """Sessiz sensörleri offline işaretle, listele."""
        now = time.time()
        offline: list[SensorRecord] = []
        async with self._lock:
            for rec in self._records.values():
                if now - rec.last_seen > self._timeout:
                    _offline.labels(
                        sensor_id=rec.sensor_id, sensor_type=rec.sensor_type,
                    ).set(1)
                    offline.append(rec)
        return offline

    def snapshot(self) -> list[dict]:
        now = time.time()
        return [
            {
                "sensor_id": r.sensor_id,
                "sensor_type": r.sensor_type,
                "last_seen_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r.last_seen)),
                "age_s": now - r.last_seen,
                "online": (now - r.last_seen) <= self._timeout,
            }
            for r in self._records.values()
        ]


async def run(nats_url: str) -> None:
    import nats

    monitor = HealthMonitor()
    nc = await nats.connect(nats_url)

    async def _on_camera(msg):
        try:
            d = json.loads(msg.data.decode())
            await monitor.on_message(d.get("sensor_id", "?"), "camera")
        except Exception:
            pass

    async def _on_rf_odid(msg):
        try:
            d = json.loads(msg.data.decode())
            await monitor.on_message(d.get("sensor_id", "?"), "rf_odid")
        except Exception:
            pass

    async def _on_rf_wifi(msg):
        try:
            d = json.loads(msg.data.decode())
            await monitor.on_message(d.get("sensor_id", "?"), "rf_wifi")
        except Exception:
            pass

    await nc.subscribe("nizam.raw.camera.>", cb=_on_camera)
    await nc.subscribe("nizam.raw.rf.odid.>", cb=_on_rf_odid)
    await nc.subscribe("nizam.raw.rf.wifi.>", cb=_on_rf_wifi)

    log.info("Sensor health monitor bağlandı (timeout=%.0fs)", OFFLINE_TIMEOUT_S)

    while True:
        offline = await monitor.check_offline()
        for rec in offline:
            log.warning("sensor offline: id=%s type=%s age=%.0fs",
                        rec.sensor_id, rec.sensor_type, time.time() - rec.last_seen)
        await asyncio.sleep(5.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="NIZAM Sensor Health Monitor")
    parser.add_argument("--nats", default="nats://localhost:6222")
    parser.add_argument("--metrics-port", type=int, default=8008)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    start_http_server(args.metrics_port)
    asyncio.run(run(args.nats))


if __name__ == "__main__":
    main()
