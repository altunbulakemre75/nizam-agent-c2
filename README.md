# NIZAM — Counter-UAS Sistemi

[![CI](https://github.com/altunbulakemre75/nizam-cop/actions/workflows/ci.yml/badge.svg)](https://github.com/altunbulakemre75/nizam-cop/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-833%20passed-brightgreen)](https://github.com/altunbulakemre75/nizam-cop/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Çok-sensörlü **counter-UAS** (anti-drone) sistemi: pasif drone tespiti, Kalman füzyonu, TAK/CoT protokolü üzerinden taktik dağıtım, **savunma-güvenli deterministic karar katmanı** ve opsiyonel otonom intercept.

COP (Common Operating Picture) prototipi olarak başladı (Anduril Lattice / Palantir Gotham ilhamlı), 8-fazlı bir roadmap ile tam sensör-to-effector counter-UAS platformuna dönüştü.

---

## Neler Var

| Katman | Nerede | Ne İşe Yarar |
|---|---|---|
| **Sensör** | [`services/detectors/`](services/detectors/) | Kamera YOLO + Remote ID (ASTM F3411) + WiFi OUI tespit, NATS yayın |
| **Füzyon** | [`services/fusion/`](services/fusion/) | 3D Kalman + IMM + Hungarian + track lifecycle + DoS korumalı |
| **Karar** | [`services/decision/`](services/decision/) | Kural engine + ROE + guardrails + LLM advisor (opsiyonel) |
| **TAK** | [`services/cot/`](services/cot/) | Track → MIL-STD-2525 CoT XML + pytak workers |
| **Otonom** | [`services/autonomy/`](services/autonomy/) | Geofence + intercept planner + MAVSDK bridge |
| **3D UI** | [`ui/`](ui/) | CesiumJS + deck.gl + sensor coverage panel |
| **Gateway** | [`services/gateway/`](services/gateway/) | NATS → WebSocket (JWT auth zorunlu) |
| **Bridge** | [`services/bridge/`](services/bridge/) | COP sim → NATS + JSONL replay publisher |
| **Knowledge** | [`services/knowledge/`](services/knowledge/) | LlamaIndex ROE RAG + TF-IDF fallback |
| **COP (v1)** | [`cop/`](cop/) | FastAPI + 25 router + 29 AI analyzer (orijinal prototip) |
| **Altyapı** | [`infra/`](infra/) | Docker: postgres + pgvector, nats, redpanda, grafana, loki, tempo, prometheus, portainer, promtail, nats-exporter |

---

## Güvenlik (Savunma Tasarımı)

Bu sistem **deterministic rule-first** mimaridir. LLM hallucination'a karşı katmanlı savunma:

| Katman | Koruma |
|---|---|
| **Rule engine** | Her karar kaynağı buradan çıkar. LLM override edemez. |
| **ROE (Rules of Engagement)** | YAML tabanlı, komutanlık düzenler. `ENGAGE` varsayılan **disabled**. |
| **Guardrails** | `input_track` (düşük güven) + `friendly_zone` (dost bölge) + `civilian_pattern` (sivil trafik) — her biri sadece **downgrade** yapabilir |
| **LLM schema** | Claude/Ollama tool schema'sında `enum: ["log","alert","handoff"]` — **ENGAGE yok** |
| **Prompt injection defense** | Allowlist field extraction + 6 pattern detector (ignore previous, system:, \[INST\], disregard...) |
| **WebSocket auth** | JWT (HS256) zorunlu, invalid → close(4401) |
| **NATS auth** | nkey tabanlı pub/sub rol ayrımı (sahte track injection engelleme) |
| **DoS defense** | Per-sensor 500 ev/s rate limit + %95 hard circuit breaker |
| **CoT TLS** | mTLS pytak enrollment (üretim zorunlu) |
| **Audit trail** | Decision schema: `reasoning` (rule) + `guardrail_reasoning` (downgrade) + `llm_raw_response` (ham LLM) + `guardrails_triggered` (hangi guard tetikledi) + PostgreSQL checkpoint |

Detaylı: [`docs/runbooks/secrets.md`](docs/runbooks/secrets.md)

---

## Hızlı Başlangıç

### 1. Altyapı (Docker)

```bash
docker compose -f infra/docker-compose.yml up -d
```

12 konteyner ayağa kalkar. Açılan portlar:

| Servis | Adres | Giriş |
|---|---|---|
| Grafana | http://localhost:5000 | admin / nizam_dev |
| Prometheus | http://localhost:11090 | — |
| Redpanda Console | http://localhost:10080 | — |
| Portainer | https://localhost:11443 | setup ilk açılışta |
| NATS | `nats://localhost:6222` | (prod'da nkey auth) |
| PostgreSQL | `localhost:7432` | nizam / nizam_dev |
| NATS Exporter | http://localhost:7777/metrics | — |

Üretim öncesi **zorunlu**: secret rotation, NATS auth etkinleştirme — bkz [runbooks](docs/runbooks/).

### 2. Python Bağımlılıklar

```bash
pip install -r requirements.txt
# İlk kez: YOLO modeli indir
python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"
```

### 3. COP Prototipi (v1)

```bash
python -m uvicorn cop.server:app --reload --port 8100
```

→ http://localhost:8100 (2D Leaflet, simülasyon senaryoları, AI panelleri)

### 4. Counter-UAS Servisleri (v2) — Production Path

```bash
# Güvenlik env'leri
export NIZAM_JWT_SECRET=$(openssl rand -base64 48)
export ANTHROPIC_API_KEY=sk-ant-...        # opsiyonel (LLM advisor)
export NIZAM_DECISION_LLM_ENABLED=true     # opsiyonel

# Kamera tespit (webcam)
python -m services.detectors.camera.yolo_service --source 0 --sensor-id cam-01

# Remote ID (mock kaynak, gerçek BT için Linux + dongle)
python -m services.detectors.rf.odid_service --sensor-id rf-01

# WiFi OUI
python -m services.detectors.rf.wifi_oui_service --sensor-id wifi-01

# Füzyon
python -m services.fusion.fusion_service

# COP → NATS köprüsü (simülasyon verisini pipeline'a bağlar)
python -m services.bridge.cop_to_nats

# Gateway (WebSocket, JWT gerekli)
python -m services.gateway.track_gateway

# CoT pipeline (FreeTAKServer varsa)
python -m services.cot --tak-host localhost --tak-port 8087

# 3D UI
cd ui && npm install && npm run dev
```

### 5. Test

```bash
python -m pytest tests/ -q     # 833 test, ~45s
```

---

## Mimari

```
                  ┌────────────────────────────────────┐
                  │  Operatör UI (CesiumJS + deck.gl   │
                  │  + sensor coverage)                │
                  │  ATAK Tabletler (CoT / mTLS)       │
                  └──────┬─────────────────────────────┘
                         │ WebSocket + JWT
                  ┌──────▼─────────────────────────────┐
                  │  Gateway (auth zorunlu)            │
                  │  + TAK Sender (mTLS)               │
                  └──────┬─────────────────────────────┘
                         │
                  ┌──────▼─────────────────────────────┐
                  │  Karar Katmanı (LangGraph 5-node)  │
                  │  classify → retrieve_roe → reason  │
                  │  → guardrail → finalize            │
                  │  (LLM sanitize + ENGAGE yasak)     │
                  └──────┬─────────────────────────────┘
                         │
                  ┌──────▼─────────────────────────────┐
                  │  Füzyon (Kalman + IMM + Hungarian) │
                  │  + rate limit + circuit breaker    │
                  └──────┬─────────────────────────────┘
                         │ NATS (nkey pub/sub auth)
         ┌───────────────┼─────────────────────────────┐
         │               │                             │
  ┌──────▼───┐  ┌────────▼─────┐  ┌─────────────────┐  │
  │ Camera   │  │ RF (ODID     │  │ COP Sim →       │  │
  │ YOLO     │  │  + WiFi OUI) │  │ NATS Bridge     │  │
  └──────────┘  └──────────────┘  └─────────────────┘  │
                                                        │
                      Otonom Kol (opsiyonel):           │
                      Intercept Planner → MAVSDK → PX4 ◄┘
                      (geofence + operatör onayı)
```

---

## Repo Yapısı

```
nizam-agent-c2/
├── AGENTS.md                   # AI agent anayasası
├── CLAUDE.md                   # COP geliştirme notları
├── docs/
│   ├── NIZAM_Envanter_ve_Plan.md   # 89 repo + 18-haftalık plan
│   ├── roe/                        # ROE doktrin markdown'ları (RAG)
│   └── runbooks/                   # camera, fusion, infra, secrets,
│       └── first-run-issues.md     # canlı deploy sorunları
├── infra/
│   ├── docker-compose.yml
│   ├── nats/nats-server.conf       # nkey auth config
│   ├── prometheus/{prometheus,alerts}.yml
│   ├── grafana/dashboards/
│   ├── promtail/config.yml
│   ├── freetakserver/Dockerfile    # self-build FTS
│   └── DEPLOY.md
├── config/
│   ├── roe/default.yaml            # varsayılan ROE (ENGAGE disabled)
│   ├── friendly_zones.yaml         # dost bölge listesi
│   └── cameras/cam-01.yaml         # kamera kalibrasyon
├── services/
│   ├── detectors/
│   │   ├── camera/                 # YOLO + DSHOW + calibration
│   │   └── rf/                     # ODID parser, WiFi OUI, live listener
│   ├── fusion/                     # KF + IMM + association + lifecycle + catalog
│   ├── bridge/                     # COP → NATS, JSONL replay
│   ├── gateway/                    # WebSocket + JWT auth
│   ├── cot/                        # CoT builder + workers + validator
│   ├── decision/                   # rules + ROE + guardrails + LLM advisor + graph
│   ├── knowledge/                  # LlamaIndex ROE RAG
│   ├── autonomy/                   # geofence + intercept + MAVSDK
│   └── schemas/                    # Pydantic modelleri
├── shared/
│   ├── auth.py                     # JWT (HS256)
│   ├── rate_limit.py               # sliding window + circuit breaker
│   ├── geo.py                      # pyproj ENU + düz-Earth fallback
│   ├── lifecycle.py                # graceful shutdown
│   ├── logging_setup.py            # JSON structured logging
│   ├── clock.py, rng.py            # test determinizmi
├── scripts/
│   ├── clone_vendors.sh            # 89 referans repo
│   ├── gen_tak_certs.sh            # mTLS CA + client cert
│   └── gen_nats_keys.sh            # NATS nkey üretici
├── ui/                             # Vite + TS + CesiumJS + deck.gl
├── cop/                            # v1 COP prototipi (623 test)
├── ai/                             # 29 AI analyzer
├── tests/                          # 833 test, ~45s
└── vendor/                         # 89 referans repo (read-only, .gitignore)
```

---

## 18 Haftalık Plan — Durum (v5)

| Faz | İçerik | Durum | Yüzde |
|---|---|---|---|
| 0 | Altyapı (Docker + monitoring + logging) | ✅ | 100% |
| 1 | Kamera tespit MVP | ✅ | 98% |
| 2 | RF tespit (ODID parser + WiFi OUI) | ✅ kod | 50% (donanım bekliyor) |
| 3 | Multi-sensor füzyon (IMM + DoS guard) | ✅ | 97% |
| 4 | 3D operatör UI | ✅ | 80% |
| 5 | TAK/CoT dağıtım | ✅ | 95% |
| 6 | AI Karar Katmanı (LangGraph + guardrails) | ✅ | 95% |
| 7 | Otonom intercept | ✅ shell | 20% (SITL bekliyor) |

Detay: [`docs/NIZAM_Envanter_ve_Plan.md`](docs/NIZAM_Envanter_ve_Plan.md)

---

## Test İstatistikleri

| Alan | Test |
|---|---|
| COP (v1) | 623 |
| Camera detector + calibration | 10 |
| RF (ODID parser + service + WiFi OUI) | 24 |
| Fusion (KF + IMM + association + lifecycle + geo + catalog) | 27 |
| Gateway (WebSocket + JWT auth) | 9 |
| CoT (builder + bridges + validator + enrichment) | 20 |
| Decision (rules + ROE + graph + LLM + guardrails + sanitize) | 49 |
| Autonomy (geofence + planner + MAVSDK) | 15 |
| Bridge + Knowledge + Integration | 15 |
| Shared (auth + rate_limit + geo) | 19 |
| **Toplam** | **833** |

```bash
python -m pytest tests/ -q    # ~45s
```

---

## Production Checklist

Pilot/üretim öncesi **zorunlu**:

- [ ] Secret rotation — bkz [`secrets.md`](docs/runbooks/secrets.md)
- [ ] NATS nkey auth etkinleştirme — [`gen_nats_keys.sh`](scripts/gen_nats_keys.sh)
- [ ] mTLS cert üretimi — [`gen_tak_certs.sh`](scripts/gen_tak_certs.sh)
- [ ] `NIZAM_JWT_SECRET` (>=32 karakter) set
- [ ] PostgreSQL backup stratejisi (pgbackrest)
- [ ] Prometheus alert sessizleştirme dışarıdan yönetim
- [ ] ROE yaml komutanlık onayı
- [ ] Friendly zones listesi doğrulama
- [ ] Kamera kalibrasyon (per-sensor GPS + intrinsics)
- [ ] YOLO drone-özel fine-tune (COCO yerine)
- [ ] Linux deploy (monitor-mode WiFi için)

---

## Lisans

MIT — bkz [`LICENSE`](LICENSE). Vendor repoları kendi lisanslarına tabi.

---

## Katkı

Savunma projesi. PR'larda özellikle güvenlik-kritik path'ler (karar katmanı, guardrails, geofence, ENGAGE kuralları, LLM sanitize) yüksek test kapsamı + TDD zorunlu. Conventional Commits.
