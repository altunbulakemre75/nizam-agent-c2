# NIZAM — Counter-UAS Sistemi

[![CI](https://github.com/altunbulakemre75/nizam-cop/actions/workflows/ci.yml/badge.svg)](https://github.com/altunbulakemre75/nizam-cop/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-719%20passed-brightgreen)](https://github.com/altunbulakemre75/nizam-cop/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Çok-sensörlü **counter-UAS** (anti-drone) sistemi: pasif drone tespiti, Kalman füzyonu, TAK/CoT protokolü üzerinden taktik dağıtım, deterministic karar katmanı ve opsiyonel otonom intercept.

COP (Common Operating Picture) prototipi olarak başladı (Anduril Lattice / Palantir Gotham ilhamlı), 8-fazlı bir roadmap ile tam sensör-to-effector counter-UAS platformuna dönüştü.

---

## Neler Var

| Katman | Nerede | Ne İşe Yarar |
|---|---|---|
| **Sensör** | [`services/detectors/`](services/detectors/) | Kamera YOLO + Remote ID (ASTM F3411) tespit, NATS'e yayın |
| **Füzyon** | [`services/fusion/`](services/fusion/) | 3D Kalman + Hungarian association + track lifecycle |
| **Karar** | [`services/decision/`](services/decision/) | Deterministic kural engine + ROE — LLM hallucination'a kapalı |
| **TAK** | [`services/cot/`](services/cot/) | Track → MIL-STD-2525 CoT XML (ATAK tablet uyumu) |
| **Otonom** | [`services/autonomy/`](services/autonomy/) | Geofence + intercept planlayıcı + MAVSDK (PX4) köprüsü |
| **3D UI** | [`ui/`](ui/) | CesiumJS + WebSocket live tracks |
| **Gateway** | [`services/gateway/`](services/gateway/) | NATS → WebSocket köprüsü |
| **COP (v1)** | [`cop/`](cop/) | FastAPI + 25 router + 29 AI analyzer (orijinal prototip) |
| **Altyapı** | [`infra/`](infra/) | Docker compose: postgres, nats, redpanda, grafana, loki, tempo |

---

## Hızlı Başlangıç

### 1. Altyapı (Docker)

```bash
docker compose -f infra/docker-compose.yml up -d
```

Açılan portlar:

| Servis | Adres | Giriş |
|---|---|---|
| Grafana | http://localhost:5000 | admin / nizam_dev |
| Prometheus | http://localhost:11090 | — |
| Redpanda Console | http://localhost:10080 | — |
| Portainer | https://localhost:11443 | ilk açılışta kurulum |
| NATS | `nats://localhost:6222` | — |
| PostgreSQL | `localhost:7432` | nizam / nizam_dev |

### 2. COP Prototipi (v1)

```bash
python -m uvicorn cop.server:app --reload --port 8100
```

→ http://localhost:8100 (2D Leaflet harita, demo senaryolar, AI panelleri)

### 3. Counter-UAS Servisleri (v2)

```bash
# Kamera tespit → NATS
python -m services.detectors.camera.yolo_service --source 0 --sensor-id cam-01

# Remote ID (Bluetooth/WiFi) tespit
python -m services.detectors.rf.odid_service --sensor-id rf-01

# Track Gateway (NATS → WebSocket UI için)
python -m services.gateway.track_gateway    # :8200

# 3D CesiumJS UI
cd ui && npm install && npm run dev          # :5173
```

### 4. Test

```bash
python -m pytest tests/ -q    # 719 test, ~45s
```

---

## Mimari

```
                  ┌────────────────────────────────────┐
                  │  Operatör UI (CesiumJS 3D Globe)   │
                  │  Leaflet 2D (eski COP)             │
                  │  ATAK Tabletler (CoT üzerinden)     │
                  └──────┬─────────────────────────────┘
                         │ WebSocket / CoT XML
                  ┌──────▼─────────────────────────────┐
                  │  Gateway + TAK Sender              │
                  │  (nizam.tracks.active → WS + TCP)  │
                  └──────┬─────────────────────────────┘
                         │
                  ┌──────▼─────────────────────────────┐
                  │  Karar Katmanı (kural-önce)         │
                  │  ThreatAssessment → ROE → Decision │
                  │  (LOG / ALERT / ENGAGE / HANDOFF)  │
                  └──────┬─────────────────────────────┘
                         │
                  ┌──────▼─────────────────────────────┐
                  │  Füzyon Motoru                     │
                  │  Kalman + Hungarian + Lifecycle    │
                  └──────┬─────────────────────────────┘
                         │ NATS nizam.raw.*
         ┌───────────────┼─────────────────────────────┐
         │               │                             │
  ┌──────▼───┐  ┌────────▼─────┐  ┌─────────────────┐  │
  │ Camera   │  │ RF (ODID)    │  │ Radar / AIS /   │  │
  │ YOLO     │  │ BT + WiFi    │  │ Generic REST    │  │
  └──────────┘  └──────────────┘  └─────────────────┘  │
                                                        │
                      Otonom Kol (opsiyonel):           │
                      Intercept Planner → MAVSDK → PX4 ◄┘
```

---

## Güvenlik Tasarım İlkeleri (Savunma Projesi)

1. **LLM asla ENGAGE kararı vermez** — tüm eylemler deterministic rule engine'den çıkar. LLM ilerideki advisor rolünde önerir, kural override edemez.
2. **Varsayılan ROE'da `ENGAGE` kuralları DEVRE DIŞI** — üretim deployment'ı komutanlığın aktifleştirmesini gerektirir.
3. **`ENGAGE` her durumda operatör onayı gerektirir** — kural yazarı unutsa bile sistem onayı zorla ekler.
4. **Geofence ihlal kontrolü** — no-fly zone içine intercept komutu planlanırsa `InterceptRefused` raise edilir.
5. **CoT TLS dışına çıkmaz** — üretim için pytak mTLS enrollment zorunlu.
6. **Audit trail** — her karar hangi ROE kuralından geldi + tetikleyici faktörler Decision nesnesinde.
7. **Public cloud deploy yok** — localhost veya VPN içi özel sunucu.

---

## Repo Yapısı

```
nizam-agent-c2/
├── AGENTS.md                   # AI agent anayasası (oturum başında okunur)
├── CLAUDE.md                   # COP geliştirme notları
├── docs/
│   └── NIZAM_Envanter_ve_Plan.md   # 89 repo + 18-haftalık faz planı
├── clone_vendors.sh            # vendor/ altına 89 referans repo çek
├── vendor/                     # 89 OSS repo (read-only, .gitignore)
├── infra/                      # docker-compose + grafana + prometheus + tempo
├── config/roe/                 # Rules of Engagement (YAML)
├── services/                   # Yeni counter-UAS servisleri
│   ├── detectors/camera/
│   ├── detectors/rf/
│   ├── fusion/
│   ├── gateway/
│   ├── cot/
│   ├── decision/
│   ├── autonomy/
│   └── schemas/
├── ui/                         # CesiumJS 3D operatör UI
├── cop/                        # Orijinal COP prototipi (FastAPI)
├── ai/                         # 29 AI analyzer (threat, fusion, anomaly, ...)
├── tests/                      # 719 pytest
└── shared/                     # clock, RNG, config (test determinizmi)
```

---

## 18 Haftalık Plan — Durum

| Faz | İçerik | Durum |
|---|---|---|
| 0 | Altyapı (Docker compose stack) | ✅ |
| 1 | Tek-sensör tespit MVP (kamera) | ✅ |
| 2 | RF tespit (OpenDroneID, DJI OcuSync) | ✅ (ODID tamam; DJI GNU Radio donanım ileride) |
| 3 | Multi-sensor füzyon (Kalman + Hungarian) | ✅ |
| 4 | 3D operatör UI (CesiumJS) | ✅ |
| 5 | TAK/CoT dağıtım (pytak uyumlu) | ✅ |
| 6 | AI Karar Katmanı (kural + opsiyonel LLM advisor) | ✅ (rule engine; LLM advisor ileride) |
| 7 | Otonom intercept (PX4 + MAVSDK) | ✅ (simülasyon; SITL + Nav2 donanım testi ileride) |

Detaylar: [`docs/NIZAM_Envanter_ve_Plan.md`](docs/NIZAM_Envanter_ve_Plan.md)

---

## Test İstatistikleri

| Alan | Test |
|---|---|
| COP (v1) | 623 |
| Camera detector | 4 |
| RF ODID (parser + service) | 15 |
| Fusion (KF + association + lifecycle) | 18 |
| Gateway (WebSocket hub) | 5 |
| CoT (builder + bridges) | 16 |
| Decision (rules + ROE + graph) | 23 |
| Autonomy (geofence + planner + MAVSDK) | 15 |
| **Toplam** | **719** |

```bash
python -m pytest tests/ -q    # ~45s
```

---

## Kod Satırları

| Bölüm | Satır |
|---|---|
| `cop/` (v1 COP) | 6,710 |
| `ai/` (29 analyzer) | 8,057 |
| `services/` (yeni v2 counter-UAS) | 1,894 |
| `shared/` + `replay/` + `scripts/` | 3,758 |
| `cop/static/` (JS + CSS + HTML) | 7,351 |
| **Production toplam** | **~27,770** |
| Testler | 7,683 |

---

## Referans Vendor Repos (89 adet, read-only)

`vendor/` altında 12 teknoloji alanında 89 OSS repo şablon olarak bulunur:
Computer Vision (YOLO, ByteTrack, OpenCV), Robotics (PX4, MAVSDK, Nav2), AI Agent
(Claude SDK, LangGraph, LlamaIndex), Local LLM (llama.cpp, Ollama, vLLM),
Data Streaming (NATS, Redpanda, Flink), Vector DB (FAISS, Qdrant, pgvector),
Monitoring (Prometheus, Grafana, Tempo), Deployment (Kamal, Portainer),
Sensor Fusion (filterpy, Sensor-Fusion-3D-MOT, smart_track),
TAK/CoT (pytak, FreeTAKServer, adsbxcot), 3D GIS (CesiumJS, deck.gl, maptalks),
SDR/RF (GNU Radio, opendroneid-core-c, SDR++).

İlk kurulum:

```bash
bash clone_vendors.sh           # ~3.2 GB, 10–30 dk
bash clone_vendors.sh 10-tak    # sadece bir alan
```

Kurallar:
- `vendor/` **read-only** — agent'lar ve geliştiriciler referans olarak okur, değiştirmez
- Düzeltme gerekiyorsa `services/` içinde wrapper yaz
- Yeni servis yazarken önce `AGENTS.md` tablosundaki şablon repoya bak

---

## Lisans

MIT — bkz [`LICENSE`](LICENSE)

Referans `vendor/` repolarının her biri kendi lisansına tabidir.

---

## Katkı

Bu proje savunma sanayi odaklı bir prototip — PR'lar öncelikle güvenlik-kritik
path'lerde (karar katmanı, geofence, ENGAGE kuralları) yüksek kodlama standardı
ve test kapsamı gerektirir.

TDD zorunlu (test önce). Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`).
