# NIZAM Geliştirici Rehberi

Teknik detaylar — mimari diyagram, repo yapısı, test komutları, deploy.

## Mimari

```
                  ┌────────────────────────────────────┐
                  │  Operatör UI (CesiumJS + deck.gl   │
                  │  + viewshed + operator panel)      │
                  │  ATAK Tabletler (CoT / mTLS)       │
                  └──────┬─────────────────────────────┘
                         │ WebSocket + JWT
                  ┌──────▼─────────────────────────────┐
                  │  Gateway (JWT auth zorunlu)        │
                  │  + TAK Sender (mTLS)               │
                  └──────┬─────────────────────────────┘
                         │
                  ┌──────▼─────────────────────────────┐
                  │  Karar Katmanı (LangGraph 5-node)  │
                  │  classify → retrieve_roe → reason  │
                  │  → guardrail → finalize            │
                  │  (prompt sanitize + ENGAGE yasak)  │
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
  │ YOLO     │  │  + WiFi OUI  │  │ NATS Bridge     │  │
  │ (CPU/GPU/│  │  + Mock Pub) │  │                 │  │
  │  TRT)    │  │              │  │                 │  │
  └──────────┘  └──────────────┘  └─────────────────┘  │
                                                        │
                      Otonom Kol (opsiyonel):           │
                      Intercept Planner → MAVSDK → PX4 ◄┘
                      (geofence + operatör onayı)
```

## Repo Yapısı

```
nizam-agent-c2/
├── AGENTS.md                   # AI agent anayasası
├── CLAUDE.md                   # COP geliştirme notları
├── docs/
│   ├── CHANGELOG.md            # sürüm günlüğü
│   ├── DEVELOPER.md            # bu dosya
│   ├── NIZAM_Envanter_ve_Plan.md
│   ├── roe/                    # ROE doktrin (RAG)
│   └── runbooks/
│       ├── camera.md
│       ├── fusion.md
│       ├── infra.md
│       ├── secrets.md
│       ├── offline-mode.md
│       └── first-run-issues.md
├── infra/
│   ├── docker-compose.yml          # 12 konteyner
│   ├── nats/nats-server.conf       # nkey auth
│   ├── prometheus/{prometheus,alerts}.yml
│   ├── grafana/dashboards/         # camera, overview
│   ├── promtail/config.yml
│   └── freetakserver/Dockerfile
├── config/
│   ├── roe/default.yaml            # ENGAGE disabled by default
│   ├── friendly_zones.yaml
│   └── cameras/{cam-01,cam-02}.yaml
├── services/
│   ├── detectors/
│   │   ├── camera/                 # YOLO + DSHOW + calibration
│   │   └── rf/                     # ODID, WiFi OUI, mock_publisher, live_listener
│   ├── fusion/                     # KF + IMM + association + lifecycle + catalog
│   ├── bridge/                     # cop_to_nats, replay_publisher
│   ├── gateway/                    # WebSocket + JWT auth
│   ├── cot/                        # builder + workers + validator + enrichment
│   ├── decision/                   # rules + ROE + guardrails + sanitize + llm_graph
│   ├── knowledge/                  # LlamaIndex ROE RAG
│   ├── health/                     # sensor_health monitor
│   ├── autonomy/                   # geofence + intercept + MAVSDK
│   └── schemas/                    # Pydantic modelleri
├── shared/
│   ├── auth.py                     # JWT (HS256)
│   ├── rate_limit.py               # sliding window + circuit breaker
│   ├── geo.py                      # pyproj ENU + düz-Earth fallback
│   ├── lifecycle.py                # graceful shutdown
│   ├── logging_setup.py            # JSON structured
│   ├── clock.py, rng.py            # test determinizmi
├── scripts/
│   ├── clone_vendors.sh            # 89 referans repo
│   ├── gen_tak_certs.sh            # mTLS CA + client cert
│   ├── gen_nats_keys.sh            # NATS nkey üretici
│   ├── export_yolo.sh              # ONNX + TensorRT export
│   ├── audit_decisions.sql         # Postgres audit queries
│   └── start_all.sh                # full-stack launcher
├── ui/                             # Vite + TS + CesiumJS + deck.gl
├── cop/                            # v1 COP prototipi
├── ai/                             # 29 AI analyzer
├── tests/                          # 833 test
└── vendor/                         # 89 referans repo (read-only, .gitignore)
```

## Test

```bash
pytest tests/ -q                               # tümü (~45s)
pytest tests/decision/ -v                      # karar katmanı
pytest tests/fusion/ -q                        # füzyon
pytest tests/integration/ -q                   # e2e (NATS'siz)
pytest tests/gateway/test_ws_auth.py -v        # JWT auth
pytest tests/decision/test_sanitize.py -v      # prompt injection
pytest tests/test_rate_limit.py -v             # DoS defense
```

## Kodlama Standardı

- Python 3.10+, tip ipuçları zorunlu
- `pytest` + AAA (Arrange-Act-Assert)
- `ruff format` + `ruff check --fix`
- Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`)
- TDD: önce test, sonra kod
- Güvenlik-kritik path'ler (karar, guardrails, geofence) %100 test coverage

## Production Checklist

Pilot öncesi **zorunlu**:

- [ ] Secret rotation — [`docs/runbooks/secrets.md`](docs/runbooks/secrets.md)
- [ ] NATS nkey auth etkinleştirme
- [ ] mTLS cert üretimi
- [ ] `NIZAM_JWT_SECRET` (>=32 karakter)
- [ ] PostgreSQL backup (pgbackrest)
- [ ] ROE yaml komutanlık onayı
- [ ] Friendly zones doğrulama
- [ ] Kamera kalibrasyon (GPS + intrinsics)
- [ ] YOLO drone fine-tune (COCO yerine)
- [ ] Linux deploy (monitor-mode WiFi için)
- [ ] 72 saat stress test

## Bir Sonraki İterasyon (v6)

Bakınız [`docs/CHANGELOG.md`](CHANGELOG.md) **Yol Haritası** bölümü.

Kritik, 1 ay içinde yapılmazsa "gerçek olmayan" işler:
1. Drone-özel YOLO fine-tune (Roboflow + Colab T4)
2. Gerçek DJI ile saha testi (video kanıt)
3. 72 saat kesintisiz kararlılık testi
4. False positive baseline ölçümü
5. Docker-compose canlı uçtan uca test
