"""YOLO kamera tespit servisi — tespitleri NATS'e yayınlar.

Kullanım:
    python -m services.detectors.camera.yolo_service \
        --source 0 --sensor-id cam-01 --nats nats://localhost:4222
"""
from __future__ import annotations

import argparse
import asyncio
import time
from typing import TYPE_CHECKING

from prometheus_client import Counter, Gauge, Histogram, start_http_server
from shared.clock import get_clock

from services.schemas.detection import BoundingBox, CameraDetectionEvent, Detection

if TYPE_CHECKING:
    import nats
    from ultralytics.engine.results import Results

# ── Prometheus metrics ────────────────────────────────────────────
_detections_total = Counter(
    "nizam_camera_detections_total",
    "Toplam tespit sayısı",
    ["sensor_id", "class_name"],
)
_inference_ms = Histogram(
    "nizam_camera_inference_ms",
    "YOLO çıkarım süresi (ms)",
    buckets=[5, 10, 20, 50, 100, 200, 500],
)
_fps = Gauge("nizam_camera_fps", "Anlık FPS", ["sensor_id"])


# ── NATS subject yardımcısı ───────────────────────────────────────
class NATSSubject:
    @staticmethod
    def camera(sensor_id: str) -> str:
        return f"nizam.raw.camera.{sensor_id}"


# ── Saf fonksiyonlar (test edilebilir) ────────────────────────────

def build_detection_event(
    result: "Results",
    sensor_id: str,
    frame_id: int,
    inference_ms: float,
) -> CameraDetectionEvent:
    """Ultralytics Results → CameraDetectionEvent dönüşümü."""
    h, w = result.orig_shape
    detections: list[Detection] = []

    if result.boxes and len(result.boxes):
        for xyxy, conf, cls in zip(
            result.boxes.xyxy.tolist(),
            result.boxes.conf.tolist(),
            result.boxes.cls.tolist(),
        ):
            detections.append(
                Detection(
                    bbox=BoundingBox(x1=xyxy[0], y1=xyxy[1], x2=xyxy[2], y2=xyxy[3]),
                    conf=float(conf),
                    class_id=int(cls),
                    class_name=result.names[int(cls)],
                )
            )

    return CameraDetectionEvent(
        sensor_id=sensor_id,
        timestamp_iso=get_clock().utcnow_iso(),
        frame_id=frame_id,
        detections=detections,
        inference_ms=inference_ms,
        frame_width=w,
        frame_height=h,
    )


async def publish_event(nc: "nats.aio.client.Client", event: CameraDetectionEvent) -> None:
    """CameraDetectionEvent'i NATS'e yayınlar."""
    subject = NATSSubject.camera(event.sensor_id)
    payload = event.model_dump_json().encode()
    await nc.publish(subject, payload)

    for det in event.detections:
        _detections_total.labels(
            sensor_id=event.sensor_id,
            class_name=det["class_name"],
        ).inc()


# ── Ana servis döngüsü ────────────────────────────────────────────

async def run(
    sensor_id: str, source: str | int, nats_url: str, model_name: str,
    shutdown: asyncio.Event | None = None,
) -> None:
    """Ana döngü — OpenCV ile frame yakala, YOLO ile tespit et, NATS'e yayınla.

    shutdown event'i set olduğunda döngü biter, kamera + NATS temiz kapanır.
    """
    import logging as _lg
    import cv2
    import nats
    from ultralytics import YOLO

    shutdown = shutdown or asyncio.Event()
    log_local = _lg.getLogger(__name__)
    _lg.basicConfig(level=_lg.INFO, format="%(asctime)s %(levelname)s %(message)s")

    log_local.info("NATS bağlanıyor: %s", nats_url)
    nc = await nats.connect(nats_url)
    log_local.info("YOLO yükleniyor: %s", model_name)
    model = YOLO(model_name)

    cap_source = int(source) if str(source).isdigit() else source
    log_local.info("Kamera açılıyor: %s", cap_source)
    cap = cv2.VideoCapture(cap_source, cv2.CAP_DSHOW if isinstance(cap_source, int) else cv2.CAP_ANY)
    if not cap.isOpened():
        log_local.error("Kamera açılamadı (source=%s)", cap_source)
        await nc.drain()
        return
    log_local.info("Kamera açık, YOLO tespit başlıyor")

    frame_id = 0
    fps_ts = time.monotonic()
    fps_count = 0
    try:
        while not shutdown.is_set():
            ok, frame = cap.read()
            if not ok:
                await asyncio.sleep(0.01)
                continue

            t0 = time.monotonic()
            results = model.predict(frame, verbose=False)
            inf_ms = (time.monotonic() - t0) * 1000.0
            _inference_ms.observe(inf_ms)

            for result in results:
                event = build_detection_event(result, sensor_id, frame_id, inf_ms)
                await publish_event(nc, event)

            frame_id += 1
            fps_count += 1
            elapsed = time.monotonic() - fps_ts
            if elapsed >= 1.0:
                _fps.labels(sensor_id=sensor_id).set(fps_count / elapsed)
                if frame_id % 30 == 0:
                    log_local.info("frame=%d fps=%.1f last_inf=%.1fms", frame_id, fps_count / elapsed, inf_ms)
                fps_count = 0
                fps_ts = time.monotonic()
            await asyncio.sleep(0)  # event loop'a nefes ver
    finally:
        log_local.info("YOLO kapatılıyor... (frame=%d)", frame_id)
        cap.release()
        await nc.drain()


def main() -> None:
    from shared.lifecycle import run_with_shutdown

    parser = argparse.ArgumentParser(description="NIZAM YOLO Kamera Servisi")
    parser.add_argument("--source", default="0", help="Kamera indeksi veya video dosyası")
    parser.add_argument("--sensor-id", default="cam-01", help="Sensör kimliği")
    parser.add_argument("--nats", default="nats://localhost:4222", help="NATS URL")
    parser.add_argument(
        "--model", default="yolov8n.pt",
        help="YOLO model — .pt (PyTorch), .onnx (ONNX Runtime), .engine (TensorRT)",
    )
    parser.add_argument("--metrics-port", type=int, default=8001, help="Prometheus port")
    parser.add_argument(
        "--device", default=None,
        help="torch device — 'cpu', 'cuda:0', veya None (auto). TensorRT için cuda zorunlu.",
    )
    args = parser.parse_args()

    start_http_server(args.metrics_port)

    async def _worker(shutdown):
        await run(args.sensor_id, args.source, args.nats, args.model, shutdown)

    asyncio.run(run_with_shutdown(_worker))


if __name__ == "__main__":
    main()
