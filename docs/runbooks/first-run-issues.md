# First-Run Issues — Canlı Deploy Logu

Bu belge sistemin **sıfırdan** çalıştırılması sırasında karşılaşılan
gerçek sorunları ve çözümleri dokümante eder. Her yeni pilot
deploy'unda güncellenmeli.

## İşlem Sırası

```bash
# 1. Repo + vendor
git clone https://github.com/altunbulakemre75/nizam-cop.git
cd nizam-cop
bash clone_vendors.sh         # opsiyonel, ~30 dk

# 2. Python env
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Altyapı
cd infra
docker compose up -d
cd ..

# 4. Testler
pytest tests/ -q              # 764+ geçmeli

# 5. Servisleri başlat (her biri ayrı terminal)
python -m uvicorn cop.server:app --port 8100
python -m services.fusion.fusion_service
python -m services.gateway.track_gateway
python -m services.detectors.camera.yolo_service --source 0 --sensor-id cam-01
python -m services.bridge.cop_to_nats

# 6. Demo senaryo
curl -X POST http://localhost:8100/api/scenarios/swarm_attack/run
```

## Karşılaşılan Gerçek Sorunlar

### 1. YOLO `model.track(source=0)` Windows'ta Takılıyor

**Semptom:** Process CPU %0'da kalıyor, inference_ms_count=0 hiç artmıyor.

**Sebep:** Ultralytics'in stream modu Windows'ta FFmpeg backend'inde bazı
sürümlerde hang yapıyor. Event loop throttle başka iş yapamadan bloke oluyor.

**Çözüm** ([`services/detectors/camera/yolo_service.py`](services/detectors/camera/yolo_service.py)):
Explicit `cv2.VideoCapture(source, cv2.CAP_DSHOW)` + async `model.predict(frame)`
döngüsü. Her frame sonrası `await asyncio.sleep(0)` yield eder.

### 2. NATS Subscribe Callback Coroutine Olmalı

**Semptom:**
```
nats.errors.Error: nats: must use coroutine for subscriptions
```

**Sebep:** `lambda m: self._on_x(m.data)` — coroutine döndürür ama
async def değil, o yüzden NATS client kabul etmez.

**Çözüm:** Her callback için `async def odid_cb(msg): ...` yaz.

### 3. Prometheus COP'u Scrape Edemiyor

**Semptom:** `nizam-cop` target Prometheus'ta "down".

**Sebep:** COP `/api/metrics` JSON döner, Prometheus exposition format bekler.

**Çözüm (WIP):** COP'a `/metrics` endpoint'i eklenmeli (prometheus_client
expose). Şimdilik COP kendi dashboard'ı için yeterli, Prometheus tarafından
dışlanabilir.

### 4. FreeTAKServer Public Image Yok

**Semptom:** `rooyca/fts-server:latest` pull hatası (registry auth).

**Çözüm:** `infra/freetakserver/Dockerfile` ile self-build:
```bash
docker compose --profile tak build freetakserver
docker compose --profile tak up -d freetakserver
```

### 5. Port Çakışmaları (Host Level)

Varsayılan portlar (5432, 8080, 3000) çok yaygın kullanılıyor. NIZAM
hepsini offset'li kullanıyor — bkz [infra.md](infra.md).

Yeni çakışma olursa:
```bash
netstat -ano | findstr ":5000"    # Windows
ss -tlnp | grep 5000              # Linux
```

### 6. Ultralytics İlk Çalıştırmada Model İndiriyor

**Semptom:** İlk başlatmada servisi ayağa kalkması 30s+ sürüyor.

**Sebep:** YOLOv8n.pt modeli (~6MB) ilk kullanım anında GitHub'dan
indiriliyor. Offline/air-gap kurulumda sorun.

**Çözüm:** Deploy öncesi manuel indir:
```bash
python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"
# yolov8n.pt repo kökünde oluşur, saklayın
```

### 7. Windows'ta `signal.SIGTERM` add_signal_handler Desteklemez

**Semptom:** `NotImplementedError` when installing signal handlers.

**Çözüm** ([`shared/lifecycle.py`](../../shared/lifecycle.py)):
`try/except NotImplementedError` ile `signal.signal()` fallback.

### 8. Docker Desktop Context "desktop-linux"

**Semptom:** `docker ps` → npipe bağlantı hatası.

**Çözüm:**
```bash
docker context use default       # Windows native engine
```

## Henüz Yaşanmamış Ama Muhtemel Sorunlar

- **Linux production:** Windows DSHOW backend yok, V4L2 kullan
- **NATS JetStream persistence:** dev'de -js flag yeterli, prod'da stream config şart
- **PostgreSQL migration:** `alembic upgrade head` otomasyonu pipeline'da yok
- **CesiumJS Ion token:** Boş bırakıldı → OSM fallback ok, 3D terrain yok
- **TLS cert rotation:** `scripts/gen_tak_certs.sh` manuel; prod'da cron + reload gerek
- **Loki log retention:** dev'de limitsiz; prod'da `retention_period: 7d`

## Deploy Checklist (Pilot Öncesi)

- [ ] `pytest tests/` full geçti
- [ ] `docker compose up -d` tüm container healthy (en az 2 dk bekle)
- [ ] Webcam tespit akıyor (`curl localhost:8001/metrics | grep detections_total`)
- [ ] Fusion track üretiyor (`grep active_tracks > 0`)
- [ ] Prometheus target'ları up (`curl localhost:11090/api/v1/targets`)
- [ ] Grafana dashboard panelleri veri gösteriyor
- [ ] YOLO modeli pre-downloaded
- [ ] `.env.production` doldurulmuş (DB şifre, Grafana, API keys)
- [ ] mTLS cert'ler üretildi (`bash scripts/gen_tak_certs.sh`)
- [ ] ROE yaml komutanlığın onayından geçmiş
- [ ] Friendly zones listesi doğrulanmış
