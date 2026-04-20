# Kamera Servisi Runbook

Port: 8001 | Metrics: `nizam_camera_*`

## Başlatma

```bash
python -m services.detectors.camera.yolo_service \
    --source 0 \
    --sensor-id cam-01 \
    --nats nats://localhost:6222 \
    --metrics-port 8001
```

`--source`: 0 (webcam), dosya yolu (test video), RTSP URL (`rtsp://...`)

## Sağlık Kontrolü

```bash
curl http://localhost:8001/metrics | grep nizam_camera_fps
# Beklenen: > 5.0
```

## Alarm: NizamCameraFpsLow (FPS < 5)

**Olası sebepler:**
1. Kamera USB kopardı
2. CPU dolu (başka process ağır iş yapıyor)
3. YOLO modeli çok büyük (yolov8m/l/x yerine n kullan)

**Aksiyon:**
```bash
# Kamera sağlığı
python -c "import cv2; cap=cv2.VideoCapture(0,cv2.CAP_DSHOW); print(cap.isOpened())"

# Daha küçük model
python -m services.detectors.camera.yolo_service --model yolov8n.pt
```

## Alarm: NizamCameraInferenceSlow (p95 > 500ms)

CPU YOLO için yeterli değil. Çözümler:
1. GPU kullan: `pip install torch --index-url https://download.pytorch.org/whl/cu121`
2. Çözünürlüğü düşür: frame'i 320x240'a resize et
3. Skip frames: her 3. frame'de inference yap

## Webcam Açılmıyor (Windows)

```python
# Explicit DSHOW backend kullanılıyor
cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
```

Hâlâ açılmıyorsa:
- Kamera başka bir uygulamada açık olabilir (Zoom, Teams, OBS)
- Windows Gizlilik ayarları → Kamera → Masaüstü uygulamaları: AÇIK
