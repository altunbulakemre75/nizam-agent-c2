"""Füzyon servisi — NATS sensör mesajlarını toplayıp track'lere birleştirir.

Input subjects:
  - nizam.raw.camera.*
  - nizam.raw.rf.odid.*
  - nizam.raw.rf.wifi.*

Output subject:
  - nizam.tracks.active  (track pydantic JSON)

Prometheus:
  - nizam_fusion_measurements_total{sensor_type}
  - nizam_fusion_active_tracks
  - nizam_fusion_tick_ms
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import time
from typing import TYPE_CHECKING

from prometheus_client import Counter, Gauge, Histogram, start_http_server

from services.fusion.track_manager import TrackManager
from services.schemas.track import Measurement, SensorType

if TYPE_CHECKING:
    import nats
    import nats.aio.msg

log = logging.getLogger(__name__)

# Default sensor → origin ENU reference point (Ankara).
# Production: her sensör kendi lat/lon+heading'ini kayıt eder.
DEFAULT_REF = (39.9334, 32.8597)
_EARTH_R = 6378137.0

_meas_total = Counter(
    "nizam_fusion_measurements_total",
    "Füzyona giren ölçüm sayısı",
    ["sensor_type"],
)
_active_gauge = Gauge("nizam_fusion_active_tracks", "Aktif track sayısı")
_tick_ms = Histogram("nizam_fusion_tick_ms", "Tick süresi (ms)", buckets=[1, 5, 10, 25, 50, 100, 250])


def latlon_to_enu(lat: float, lon: float, ref_lat: float, ref_lon: float) -> tuple[float, float]:
    """Küçük sahada (~10 km) düz-Earth lat/lon → ENU (east, north metre)."""
    d_lat = math.radians(lat - ref_lat)
    d_lon = math.radians(lon - ref_lon)
    east = d_lon * _EARTH_R * math.cos(math.radians(ref_lat))
    north = d_lat * _EARTH_R
    return east, north


def enu_to_latlon(east: float, north: float, ref_lat: float, ref_lon: float) -> tuple[float, float]:
    d_lat = math.degrees(north / _EARTH_R)
    d_lon = math.degrees(east / (_EARTH_R * math.cos(math.radians(ref_lat))))
    return ref_lat + d_lat, ref_lon + d_lon


def odid_to_measurement(msg: dict, ref_lat: float, ref_lon: float) -> Measurement | None:
    """ODIDEvent dict → Measurement. Location yoksa None."""
    loc = msg.get("location")
    if loc is None:
        return None
    e, n = latlon_to_enu(float(loc["latitude"]), float(loc["longitude"]), ref_lat, ref_lon)
    alt = loc.get("altitude_geo_m") or loc.get("altitude_baro_m") or 0.0
    basic = msg.get("basic_id") or {}
    return Measurement(
        sensor_id=msg["sensor_id"],
        sensor_type=SensorType.RF_ODID,
        timestamp_iso=msg["timestamp_iso"],
        x=e, y=n, z=float(alt),
        sigma_x=3.0, sigma_y=3.0, sigma_z=8.0,  # GPS ~3m
        uas_id=basic.get("uas_id"),
        rssi_dbm=msg.get("rssi_dbm"),
    )


def camera_to_measurements(
    msg: dict, ref_lat: float, ref_lon: float,
    sensor_lat: float, sensor_lon: float,
    bearing_deg: float = 0.0,
) -> list[Measurement]:
    """Kamera tespitlerini ölçümlere çevir.

    Basitleştirme: bbox merkezinden bearing + nominal range ile ENU konumu
    türetilir. Üretimde her kameranın kalibrasyonu gerekli (intrinsics +
    extrinsics + DEM). Bu dev-placeholder.
    """
    measurements: list[Measurement] = []
    sensor_e, sensor_n = latlon_to_enu(sensor_lat, sensor_lon, ref_lat, ref_lon)
    for det in msg.get("detections", []):
        nominal_range_m = 250.0  # camera'dan ~250m'de drone varsayımı
        bearing_rad = math.radians(bearing_deg)
        x = sensor_e + nominal_range_m * math.sin(bearing_rad)
        y = sensor_n + nominal_range_m * math.cos(bearing_rad)
        measurements.append(
            Measurement(
                sensor_id=msg["sensor_id"],
                sensor_type=SensorType.CAMERA,
                timestamp_iso=msg["timestamp_iso"],
                x=x, y=y, z=100.0,
                sigma_x=30.0, sigma_y=30.0, sigma_z=50.0,  # kamera menzil belirsiz
                class_name=det.get("class_name"),
                class_conf=det.get("conf"),
            )
        )
    return measurements


class FusionService:
    """Füzyon servisinin orkestrasyonu — NATS subscriber + tick loop + publisher."""

    def __init__(
        self,
        nats_url: str,
        ref_lat: float = DEFAULT_REF[0],
        ref_lon: float = DEFAULT_REF[1],
        tick_dt: float = 0.1,
    ) -> None:
        self.nats_url = nats_url
        self.ref_lat = ref_lat
        self.ref_lon = ref_lon
        self.tick_dt = tick_dt
        self.manager = TrackManager()
        self._queue: asyncio.Queue[Measurement] = asyncio.Queue(maxsize=10_000)
        self._nc = None

    async def _on_odid(self, raw: bytes) -> None:
        try:
            msg = json.loads(raw.decode())
        except json.JSONDecodeError:
            return
        meas = odid_to_measurement(msg, self.ref_lat, self.ref_lon)
        if meas is not None:
            await self._queue.put(meas)
            _meas_total.labels(sensor_type=meas.sensor_type.value).inc()

    async def _on_camera(self, raw: bytes) -> None:
        try:
            msg = json.loads(raw.decode())
        except json.JSONDecodeError:
            return
        # Üretim: sensor_id → calibration lookup. Şimdilik ref point'ten 0 bearing.
        for meas in camera_to_measurements(
            msg, self.ref_lat, self.ref_lon,
            sensor_lat=self.ref_lat, sensor_lon=self.ref_lon, bearing_deg=0.0,
        ):
            await self._queue.put(meas)
            _meas_total.labels(sensor_type=meas.sensor_type.value).inc()

    async def _tick_loop(self) -> None:
        while True:
            t0 = time.monotonic()
            batch: list[Measurement] = []
            while not self._queue.empty():
                batch.append(self._queue.get_nowait())

            tracks = self.manager.step(batch, dt=self.tick_dt)
            _active_gauge.set(len(tracks))

            if self._nc is not None:
                for track in tracks:
                    # ENU → lat/lon dönüşümü payload'a eklenir
                    lat, lon = enu_to_latlon(track.x, track.y, self.ref_lat, self.ref_lon)
                    payload = track.model_dump()
                    payload["latitude"] = lat
                    payload["longitude"] = lon
                    payload["altitude"] = track.z
                    await self._nc.publish(
                        "nizam.tracks.active", json.dumps(payload).encode()
                    )

            dt_ms = (time.monotonic() - t0) * 1000.0
            _tick_ms.observe(dt_ms)
            await asyncio.sleep(self.tick_dt)

    async def run(self) -> None:
        import nats

        self._nc = await nats.connect(self.nats_url)
        await self._nc.subscribe("nizam.raw.rf.odid.>", cb=lambda m: self._on_odid(m.data))
        await self._nc.subscribe("nizam.raw.camera.>", cb=lambda m: self._on_camera(m.data))
        log.info("Fusion service listening on NATS %s", self.nats_url)
        await self._tick_loop()


def main() -> None:
    parser = argparse.ArgumentParser(description="NIZAM Füzyon Servisi")
    parser.add_argument("--nats", default="nats://localhost:6222")
    parser.add_argument("--ref-lat", type=float, default=DEFAULT_REF[0])
    parser.add_argument("--ref-lon", type=float, default=DEFAULT_REF[1])
    parser.add_argument("--tick-dt", type=float, default=0.1)
    parser.add_argument("--metrics-port", type=int, default=8003)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    start_http_server(args.metrics_port)

    service = FusionService(args.nats, args.ref_lat, args.ref_lon, args.tick_dt)
    asyncio.run(service.run())


if __name__ == "__main__":
    main()
