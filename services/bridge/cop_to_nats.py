"""COP → NATS köprüsü.

Eski COP prototipi (cop/) kendi sahte senaryo track'lerini üretir ama
yeni counter-UAS pipeline'ına (NATS bus) yayınlamaz. Bu köprü COP'un
HTTP API'sinden polling ile track'leri çeker ve `nizam.raw.camera.cop`
subject'ine ölçüm olarak yayınlar.

Böylece:
  cop scenario → cop REST API → bu köprü → NATS → fusion → tracks.active
  webcam YOLO   →                            → NATS → fusion → aynı track'ler
                                             (iki kaynak aynı motor)

Kullanım:
    python -m services.bridge.cop_to_nats --nats nats://localhost:6222
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging

import httpx
from prometheus_client import Counter, start_http_server
from shared.clock import get_clock

from services.schemas.detection import BoundingBox, CameraDetectionEvent, Detection

log = logging.getLogger(__name__)

_tracks_pulled = Counter("nizam_bridge_cop_tracks_total", "COP'tan çekilen track sayısı")
_publish_total = Counter("nizam_bridge_cop_publish_total", "NATS'e yayınlanan mesaj")


async def pull_cop_tracks(client: httpx.AsyncClient, cop_url: str) -> list[dict]:
    """COP /api/tracks endpoint'inden aktif track listesini çek."""
    try:
        r = await client.get(f"{cop_url}/api/tracks", timeout=2.0)
        r.raise_for_status()
        data = r.json()
        # API ya {tracks:[...]} ya da düz list dönebilir
        if isinstance(data, list):
            return data
        return data.get("tracks", [])
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("COP poll hatası: %s", exc)
        return []


def track_to_camera_event(track: dict) -> CameraDetectionEvent:
    """COP track'i sahte bir CameraDetectionEvent'e sar — fusion'a aynı kapıdan girer.

    NOT: 'intent' ve 'lat/lon'u metadata olarak bbox kullanarak taşır (hack).
    Üretimde COP'un lat/lon'u doğrudan fusion'a farklı bir subject'le (örn.
    nizam.raw.sim.cop) göndermek daha temiz. Şimdilik en az kod + en hızlı demo.
    """
    lat = float(track.get("latitude", track.get("lat", 0.0)))
    lon = float(track.get("longitude", track.get("lon", 0.0)))
    alt = float(track.get("altitude", track.get("alt", 100.0)))
    confidence = float(track.get("confidence", track.get("conf", 0.7)))
    class_name = str(track.get("intent", track.get("class", "drone")))

    # lat/lon'u bbox metadata olarak fusion bridge'e iletebiliriz.
    # Normal bbox anlamı yok; fusion düz JSON tüketmek üzere özel bir
    # "sim_cop" varyantı alacak. Şimdilik bbox yokmuş gibi boş.
    detection = Detection(
        bbox=BoundingBox(x1=lat, y1=lon, x2=lat, y2=lon),  # geçici tünel
        conf=confidence, class_id=0, class_name=class_name,
    )
    return CameraDetectionEvent(
        sensor_id="cop-sim",
        timestamp_iso=get_clock().utcnow_iso(),
        frame_id=int(track.get("tick", 0)),
        detections=[detection],
        inference_ms=0.0,
        frame_width=1920, frame_height=1080,
    )


async def run(cop_url: str, nats_url: str, rate_hz: float) -> None:
    import nats

    log.info("COP: %s  NATS: %s", cop_url, nats_url)
    nc = await nats.connect(nats_url)
    client = httpx.AsyncClient()
    interval = 1.0 / rate_hz

    try:
        while True:
            tracks = await pull_cop_tracks(client, cop_url)
            _tracks_pulled.inc(len(tracks))

            for track in tracks:
                # Doğrudan sim subject'ine JSON olarak gönder — fusion için
                # ayrı bir subscription bekliyor olacak
                payload = {
                    "sensor_id": "cop-sim",
                    "timestamp_iso": get_clock().utcnow_iso(),
                    "track_id": str(track.get("id", "?")),
                    "latitude": float(track.get("latitude", track.get("lat", 0.0))),
                    "longitude": float(track.get("longitude", track.get("lon", 0.0))),
                    "altitude": float(track.get("altitude", track.get("alt", 100.0))),
                    "vx": float(track.get("vx", 0.0)),
                    "vy": float(track.get("vy", 0.0)),
                    "vz": float(track.get("vz", 0.0)),
                    "class_name": str(track.get("intent", "drone")),
                    "confidence": float(track.get("confidence", 0.7)),
                }
                await nc.publish(
                    "nizam.raw.sim.cop",
                    json.dumps(payload).encode(),
                )
                _publish_total.inc()

            await asyncio.sleep(interval)
    finally:
        await nc.drain()
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="COP → NATS köprüsü")
    parser.add_argument("--cop-url", default="http://localhost:8100")
    parser.add_argument("--nats", default="nats://localhost:6222")
    parser.add_argument("--rate", type=float, default=2.0, help="Hz (saniyede poll sayısı)")
    parser.add_argument("--metrics-port", type=int, default=8005)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    start_http_server(args.metrics_port)
    asyncio.run(run(args.cop_url, args.nats, args.rate))


if __name__ == "__main__":
    main()
