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
from shared.geo import enu_to_latlon as _geo_enu_to_latlon
from shared.geo import latlon_to_enu as _geo_latlon_to_enu

if TYPE_CHECKING:
    import nats
    import nats.aio.msg

log = logging.getLogger(__name__)

DEFAULT_REF = (39.9334, 32.8597)

_meas_total = Counter(
    "nizam_fusion_measurements_total",
    "Füzyona giren ölçüm sayısı",
    ["sensor_type"],
)
_active_gauge = Gauge("nizam_fusion_active_tracks", "Aktif track sayısı")
_tick_ms = Histogram("nizam_fusion_tick_ms", "Tick süresi (ms)", buckets=[1, 5, 10, 25, 50, 100, 250])


def latlon_to_enu(lat: float, lon: float, ref_lat: float, ref_lon: float) -> tuple[float, float]:
    """Lat/lon → ENU (east, north metre).

    pyproj varsa tam küresel geometri; yoksa düz-Earth fallback. shared.geo
    her iki yolu da yönetir — fusion service bu detayı bilmek zorunda değil.
    """
    e, n, _ = _geo_latlon_to_enu(lat, lon, ref_lat, ref_lon)
    return e, n


def enu_to_latlon(east: float, north: float, ref_lat: float, ref_lon: float) -> tuple[float, float]:
    lat, lon, _ = _geo_enu_to_latlon(east, north, ref_lat, ref_lon)
    return lat, lon


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
    sensor_lat: float | None = None, sensor_lon: float | None = None,
    bearing_deg: float = 0.0,
) -> list[Measurement]:
    """Kamera tespitlerini ölçümlere çevir.

    Kalibrasyon dosyası varsa (`config/cameras/{sensor_id}.yaml`) bbox
    → lat/lon projeksiyonu yapılır. Yoksa sabit nominal range fallback.
    """
    from services.detectors.camera.calibration import (
        load_calibration, project_bbox_to_position,
    )

    measurements: list[Measurement] = []
    sensor_id = msg["sensor_id"]
    frame_w = int(msg.get("frame_width", 640))
    frame_h = int(msg.get("frame_height", 480))

    calib = load_calibration(sensor_id)

    for det in msg.get("detections", []):
        bbox = det.get("bbox", {})
        try:
            lat, lon, alt = project_bbox_to_position(
                float(bbox["x1"]), float(bbox["y1"]),
                float(bbox["x2"]), float(bbox["y2"]),
                frame_w, frame_h, calib,
            )
            e, n = latlon_to_enu(lat, lon, ref_lat, ref_lon)
            z = alt
        except (KeyError, TypeError, ValueError):
            # Fallback: sabit nominal range ekseninde
            sensor_e, sensor_n = latlon_to_enu(
                sensor_lat or calib.latitude, sensor_lon or calib.longitude,
                ref_lat, ref_lon,
            )
            bearing_rad = math.radians(bearing_deg)
            e = sensor_e + calib.nominal_range_m * math.sin(bearing_rad)
            n = sensor_n + calib.nominal_range_m * math.cos(bearing_rad)
            z = calib.altitude_m

        measurements.append(
            Measurement(
                sensor_id=sensor_id,
                sensor_type=SensorType.CAMERA,
                timestamp_iso=msg["timestamp_iso"],
                x=e, y=n, z=z,
                sigma_x=30.0, sigma_y=30.0, sigma_z=50.0,
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

    async def _on_sim_cop(self, raw: bytes) -> None:
        """COP simulator bridge'den gelen track'ler. Direkt lat/lon taşır."""
        try:
            msg = json.loads(raw.decode())
        except json.JSONDecodeError:
            return
        e, n = latlon_to_enu(
            float(msg["latitude"]), float(msg["longitude"]),
            self.ref_lat, self.ref_lon,
        )
        meas = Measurement(
            sensor_id=msg.get("sensor_id", "cop-sim"),
            sensor_type=SensorType.RADAR,
            timestamp_iso=msg["timestamp_iso"],
            x=e, y=n, z=float(msg.get("altitude", 100.0)),
            sigma_x=5.0, sigma_y=5.0, sigma_z=10.0,
            class_name=msg.get("class_name"),
            class_conf=float(msg.get("confidence", 0.7)),
        )
        await self._queue.put(meas)
        _meas_total.labels(sensor_type=meas.sensor_type.value).inc()

    async def _tick_loop(self, shutdown: asyncio.Event) -> None:
        while not shutdown.is_set():
            t0 = time.monotonic()
            batch: list[Measurement] = []
            while not self._queue.empty():
                batch.append(self._queue.get_nowait())

            tracks = self.manager.step(batch, dt=self.tick_dt)
            _active_gauge.set(len(tracks))

            if self._nc is not None:
                for track in tracks:
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
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=self.tick_dt)
            except asyncio.TimeoutError:
                continue

    async def run(self, shutdown: asyncio.Event | None = None) -> None:
        import nats

        shutdown = shutdown or asyncio.Event()
        self._nc = await nats.connect(self.nats_url)

        async def odid_cb(msg):
            await self._on_odid(msg.data)

        async def camera_cb(msg):
            await self._on_camera(msg.data)

        async def sim_cop_cb(msg):
            await self._on_sim_cop(msg.data)

        await self._nc.subscribe("nizam.raw.rf.odid.>", cb=odid_cb)
        await self._nc.subscribe("nizam.raw.camera.>", cb=camera_cb)
        await self._nc.subscribe("nizam.raw.sim.cop", cb=sim_cop_cb)
        log.info("Fusion service listening on NATS %s", self.nats_url)

        try:
            await self._tick_loop(shutdown)
        finally:
            log.info("Fusion service shutting down...")
            await self._nc.drain()


def main() -> None:
    parser = argparse.ArgumentParser(description="NIZAM Füzyon Servisi")
    parser.add_argument("--nats", default="nats://localhost:6222")
    parser.add_argument("--ref-lat", type=float, default=DEFAULT_REF[0])
    parser.add_argument("--ref-lon", type=float, default=DEFAULT_REF[1])
    parser.add_argument("--tick-dt", type=float, default=0.1)
    parser.add_argument("--metrics-port", type=int, default=8003)
    args = parser.parse_args()

    from shared.lifecycle import run_with_shutdown

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    start_http_server(args.metrics_port)

    service = FusionService(args.nats, args.ref_lat, args.ref_lon, args.tick_dt)
    asyncio.run(run_with_shutdown(service.run))


if __name__ == "__main__":
    main()
