# NIZAM — Counter-UAS (Anti-Drone) C2 Sistemi

[![CI](https://github.com/altunbulakemre75/nizam-cop/actions/workflows/ci.yml/badge.svg)](https://github.com/altunbulakemre75/nizam-cop/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-833%20passed-brightgreen)](https://github.com/altunbulakemre75/nizam-cop/actions)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Problem

Drone tehdidi her gün büyüyor. Türkiye'deki havalimanı, sınır bölgesi ve kritik tesislerin önemli bir kısmı tek-sensörlü, pahalı ve kapalı-kaynak counter-UAS çözümleriyle korunuyor. Anduril Lattice bedava değil; Havelsan/Aselsan ürünleri açık değil; operatör gerçek-zamanlı taktik farkındalık için ATAK kullanıyor ama drone tespiti Lattice mimarisinden kopuk.

**NIZAM açığı kapatır:** açık-kaynak, savunma-güvenli, çok-sensörlü, ATAK-uyumlu, yerli geliştirme.

## Çözüm

Anduril Lattice'in açık-kaynak alternatifi — **Türk savunma sanayiine özel tasarlanmış**:

- **Pasif tespit**: kamera (YOLO) + Remote ID (Bluetooth/WiFi) + opsiyonel radar
- **Multi-sensor füzyon**: Kalman IMM + Hungarian association — iki sensör aynı drone'u gördüğünde tek track
- **Savunma-güvenli karar**: LLM önerir, **rule engine karar verir**, ENGAGE varsayılan kapalı
- **ATAK entegrasyonu**: FreeTAKServer + pytak + mTLS, operatör tabletinde anlık CoT
- **Offline çalışır**: Ollama yerel LLM, air-gap deploy bundled
- **Tüm veri Türkiye'de kalır**: Anthropic opsiyonel, Ollama varsayılan

## Demo

| Katman | URL / Erişim |
|---|---|
| 3D Harita + operatör paneli | http://localhost:5173 |
| Komuta merkezi (v1 COP) | http://localhost:8100 |
| Metrikler dashboard | http://localhost:5000 (Grafana) |
| ATAK tablet | mTLS üzerinden CoT TCP |

Ekran kaydı / canlı demo: `docs/demo/` (sonraki sürümde)

## Referans / İlham

| Proje | NIZAM'da rolü |
|---|---|
| **Anduril Lattice** | Mimari ilham — çok-sensör + AI karar katmanı |
| **Palantir Gotham** | Lineage + audit trail + decision provenance |
| **CesiumJS + deck.gl** | 3D operatör UI |
| **FreeTAKServer + ATAK** | Taktik dağıtım katmanı |
| **OpenDroneID (ASTM F3411)** | RF tespit standardı |
| **filterpy + smart_track** | Kalman + IMM implementasyon deseni |

89 açık-kaynak referans repo (`vendor/`) 12 teknoloji alanında NIZAM'ın şablon iskeleti — Antigravity "AI agent"'larla hızlı entegrasyon.

## Hızlı Başlangıç (5 dakika)

```bash
# 1. Docker altyapı
docker compose -f infra/docker-compose.yml up -d

# 2. Python bağımlılıklar
pip install -r requirements.txt
python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"  # ilk kez

# 3. Tüm servisleri başlat (webcam + simülasyon + gateway + bridge)
bash scripts/start_all.sh

# 4. Senaryo
curl -X POST http://localhost:8100/api/scenarios/swarm_attack/run

# 5. Tarayıcı
# http://localhost:8100          → 2D harita, senaryo tests
# http://localhost:5000          → Grafana (admin/nizam_dev)
# http://localhost:5173          → 3D UI (cd ui && npm install && npm run dev)
```

**Multi-kamera test:** `bash scripts/start_all.sh --multi`
**Mock RF ekleme:** `bash scripts/start_all.sh --mock-rf`

## Teknik Özet

**Faz durumu (v5):**

| Faz | Ne | Yüzde |
|---|---|---|
| 0 — Altyapı | Docker 12 servis | 100% |
| 1 — Kamera tespit | YOLO + NATS + graceful shutdown | 98% |
| 2 — RF tespit | ODID parser + WiFi OUI (mock publisher) | 50% (donanım bekliyor) |
| 3 — Füzyon | Kalman + IMM + Hungarian + DoS guard | 97% |
| 4 — UI | CesiumJS + deck.gl + operator panel | 85% |
| 5 — TAK/CoT | Builder + pytak workers + mTLS | 95% |
| 6 — AI Karar | LangGraph 5-node + guardrails + sanitize | 95% |
| 7 — Intercept | Geofence + planner (SITL opsiyonel) | 20% |

**Güvenlik katmanları:** rule-first + ROE + 3 guardrail + LLM enum yasağı + prompt injection defense + JWT WS auth + NATS nkey + DoS rate limit + mTLS CoT + audit trail.

**Test:** 833/833 geçiyor. Detay: [`docs/CHANGELOG.md`](docs/CHANGELOG.md)

**Dokümantasyon:**
- [`docs/NIZAM_Envanter_ve_Plan.md`](docs/NIZAM_Envanter_ve_Plan.md) — 18-haftalık mimari plan
- [`docs/runbooks/`](docs/runbooks/) — deploy + sorun giderme
- [`docs/runbooks/secrets.md`](docs/runbooks/secrets.md) — Vault + rotation policy
- [`docs/runbooks/offline-mode.md`](docs/runbooks/offline-mode.md) — air-gap deploy

## Geliştirici Detayları

Repo yapısı, test komutları, mimari diyagram için: [`docs/DEVELOPER.md`](docs/DEVELOPER.md)

## İletişim

- **Geliştirici:** Emre Altunbulak
- **E-posta:** altunbulakemre75@gmail.com
- **GitHub:** https://github.com/altunbulakemre75/nizam-cop

NIZAM pilot kurulum, danışmanlık veya özelleştirme için iletişime geçin.

## Lisans

MIT — Vendor repo'ları kendi lisanslarına tabi.
