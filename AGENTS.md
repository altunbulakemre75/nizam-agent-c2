# NIZAM — Counter-UAS System

> Bu dosya Antigravity ve Claude Code tarafından her session başında okunur.
> Projede çalışacak her agent önce buradaki kuralları ve bağlamı özümser.

## Proje Tanımı

NIZAM, çok-sensörlü (görüntü + RF + derinlik) pasif drone tespiti, Kalman füzyonu,
TAK/CoT protokolü üzerinden taktik dağıtım ve opsiyonel otonom intercept
yetenekleri içeren bir counter-UAS sistemidir.

**Ana belge:** `docs/NIZAM_Envanter_ve_Plan.md` — bu dosya projenin anayasasıdır.
Mimari, faz planı ve repo envanteri burada. Herhangi bir kararda önce oraya bak.

**Mevcut COP prototipi:** `nizam-agent-c2` (bu repo) — FastAPI + WebSocket + 29 AI
analyzer + 623 test. Faz 6 (AI karar katmanı) için doğrudan referans alınacak.

## Teknoloji Stack'i (89 repo, 12 alan)

Tüm referans repolar `vendor/` altında alan bazlı organize:

```
vendor/
├── 01-cv/          # YOLO, Supervision, ByteTrack, Norfair, OpenCV, RF-DETR
├── 02-robotics/    # PX4, MAVSDK, Nav2, RTAB-Map, Isaac vSLAM
├── 03-ai-agent/    # Claude SDK, LangGraph, LlamaIndex, CrewAI, Dify
├── 04-local-llm/   # llama.cpp, Ollama, vLLM, LocalAI, Jan
├── 05-streaming/   # NATS, Redpanda, AutoMQ, Flink, Pulsar
├── 06-vector-db/   # Qdrant, pgvector, FAISS, Milvus, Chroma
├── 07-monitoring/  # Prometheus, Grafana, VictoriaMetrics, Loki, Tempo
├── 08-deployment/  # Kamal, Portainer, Komodo, Coolify, Dokku
├── 09-fusion/      # filterpy, SMART-TRACK, Sensor-Fusion-3D-MOT, RAFT
├── 10-tak/         # pytak, FreeTAKServer, GoATAK, cotproxy, adsbxcot
├── 11-3d-gis/      # CesiumJS, deck.gl, Kepler.gl, MapLibre, maptalks
└── 12-sdr-rf/      # GNU Radio, opendroneid-core-c, dji_droneid, SDR++
```

**vendor/ READ-ONLY — agent'lar referans olarak okur, değiştirmez.**
NIZAM kodu `services/`, `ui/`, `infra/` altında yazılır.

## Kritik Şablonlar (kopyalanıp uyarlanacak)

Agent yeni servis yazarken ÖNCE bu şablonlara bakmalı:

| Yazılacak servis | Şablon repo | Şablon dosya |
|---|---|---|
| ODID → CoT köprüsü | `vendor/10-tak/adsbxcot-main` | `adsbxcot/functions.py`, `classes.py` |
| Fusion track → CoT | `vendor/10-tak/adsbxcot-main` | Aynı desen |
| Kalman füzyon servisi | `vendor/09-fusion/smart_track-main` | `smart_track/detection_node.py` |
| 3D MOT track manager | `vendor/09-fusion/Sensor-Fusion-3D-MOT-main` | `student/trackmanagement.py` |
| LangGraph karar grafiği | `vendor/03-ai-agent/langgraph-main` | `examples/` |
| Viewshed hesap (UI) | `vendor/11-3d-gis/maptalks.js-master` | `packages/analysis/src/ViewshedAnalysis.js` |
| CoT XSD validation | `vendor/10-tak/AndroidTacticalAssaultKit-CIV-main` | `takcot/xsd/`, `takcot/examples/*.cot` |

Şablon kopyalarken: yapıyı koru, isim/alan adlarını uyarla, orijinal lisansa uy.

## Monorepo Yapısı

```
nizam-agent-c2/
├── AGENTS.md                 # bu dosya
├── CLAUDE.md                 # mevcut COP geliştirme notları
├── README.md
├── docs/
│   ├── NIZAM_Envanter_ve_Plan.md   # ana plan belgesi
│   ├── architecture.md
│   └── core_definition.md
├── infra/
│   ├── docker-compose.yml    # postgres, nats, redpanda, grafana...
│   ├── kamal/
│   └── grafana/dashboards/
├── services/
│   ├── detectors/
│   │   ├── camera/           # Faz 1 (YOLO → NATS)
│   │   └── rf/               # Faz 2 (OpenDroneID, dji_droneid)
│   ├── fusion/               # Faz 3 (filterpy + SMART-TRACK pattern)
│   ├── cot/                  # Faz 5 (pytak workers, adsbxcot şablonu)
│   ├── decision/             # Faz 6 (LangGraph + Claude SDK)
│   └── autonomy/             # Faz 7 (PX4 + MAVSDK, opsiyonel)
├── ui/                       # Faz 4 (CesiumJS + deck.gl + maptalks)
├── cop/                      # MEVCUT — FastAPI COP prototipi
├── ai/                       # MEVCUT — 29 AI analyzer
├── tests/
└── vendor/                   # 89 referans repo (read-only)
```

## Kodlama Kuralları

### Python
- **Python 3.12+**, `uv` paket yönetici
- **Tip ipuçları zorunlu**, `Any` yasak, `TypedDict` tercih edilir
- Format: `ruff format`, lint: `ruff check --fix`, tip: `mypy --strict`
- Test: `pytest` + Arrange-Act-Assert deseni
- Async varsayılan — `asyncio` + `anyio`

### TypeScript
- Strict mode, `any` yasak
- `pnpm` paket yönetici
- Format: Prettier, lint: ESLint

### Mesaj Şemaları
- NATS/Redpanda mesajları: **Pydantic v2** modelleri, tek kaynak `services/schemas/`

### Gözlemlenebilirlik
- Her servis metrik yayınlar: `detection_fps`, `fusion_latency_ms`, `track_count`
- Her servis health endpoint: `/health`, `/metrics`

## Agent Davranış Kuralları

1. **Önce plan belgesini oku.** `docs/NIZAM_Envanter_ve_Plan.md` — hangi fazdayız?
2. **Şablon varsa sıfırdan yazma.** `vendor/` altında zaten çalışan kod var.
3. **Bir seferde bir faz.** Faz 1 bitmeden Faz 2'ye geçme.
4. **TDD.** Önce test yaz, sonra kod.
5. **Büyük dosya yazma.** Fonksiyon <40 satır, dosya <300 satır tercih edilir.
6. **Sır yazma.** Tüm kimlik bilgileri `.env` dosyasından.
7. **Commit mesajları:** Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`).
8. **vendor/ değiştirme.** Sadece oku.

## Güvenlik Kritik

NIZAM bir savunma/güvenlik sistemi:

- **Hiçbir test verisinde gerçek hedef koordinatı kullanma**
- **CoT mesajları TLS olmadan ağa çıkmaz** (mTLS + pytak enrollment)
- **Intercept komutları guardrail'siz çalışmaz** (ROE check zorunlu)
- **LLM karar çıktıları SGLang constrained schema ile zorlanır**
- **PII / operatör kimliği log'lara düşmez**

## Şu Anki Faz

**Faz 0: Altyapı kurulumu.**

Hedef: `docker compose up` ile tüm altyapı ayakta.
Bitince Faz 1 (tek kamera YOLO → NATS) başlar.

## Geliştirme Ortamı

```bash
# COP prototipi (mevcut)
python -m uvicorn cop.server:app --reload --port 8100
python -m pytest tests/ -q   # 623 test

# Yeni servisler (Faz 1+)
docker compose -f infra/docker-compose.yml up -d
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
```

## Referanslar

- Ana plan: `docs/NIZAM_Envanter_ve_Plan.md`
- COP prototip notları: `CLAUDE.md`
- Vendor clone script: `clone_vendors.sh`

---

*Bu dosya projenin anayasasıdır — değişiklik gerekiyorsa önce burayı güncelle.*
