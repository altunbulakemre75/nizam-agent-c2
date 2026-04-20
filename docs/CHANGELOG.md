# NIZAM Counter-UAS — Değişiklik Günlüğü

Ters kronolojik. Her büyük commit gruplaması bir başlık.

---

## [v5] 2026-04-20 — Güvenlik Sertleştirme + Eksik Kapatma (833 test)

### Güvenlik
- **JWT auth**: WebSocket gateway JWT zorunlu (HS256), NIZAM_JWT_SECRET env, invalid → close(4401)
- **NATS nkey auth**: publisher/subscriber rol ayrımı, pub sadece raw.>+tracks.>, sub sadece nizam.>
- **Prompt injection defense**: allowlist field extraction + 6 pattern detector, LLM advisor sanitize kullanıyor
- **Rate limit + circuit breaker**: per-sensor 500 ev/s sliding window + %95 hard queue breaker
- **Secret management runbook**: Vault / Docker secrets / rotation policy / sızıntı yanıtı
- **mTLS CA script**: gen_tak_certs.sh (self-signed CA + client cert + P12 bundle)
- **NATS nkey script**: gen_nats_keys.sh

### Karar katmanı (Faz 6)
- **LangGraph 5-node** state machine: classify → retrieve_roe → reason → guardrail → finalize
- **Ollama fallback**: Anthropic yoksa llama3.1 localhost:11434 (offline saha)
- **Guardrails module**: input_track + friendly_zone + civilian_pattern (ASLA upgrade, sadece downgrade)
- **LLM schema**: ENGAGE enum yasak (Claude + Ollama)
- **Audit trail**: llm_raw_response dict + guardrail_reasoning ayrı field + PostgreSQL checkpoint
- **ROE RAG**: LlamaIndex + TF-IDF fallback (docs/roe/ markdown)

### Füzyon (Faz 3)
- **IMM filter**: 2-model interacting multiple model (düz + manevra)
- **FAISS drone catalog**: 6 bilinen drone + numpy fallback lookup
- **pyproj ENU**: +proj=ortho (taşınabilir), düz-Earth fallback
- **Graceful shutdown**: subscriptions unsubscribe + queue drain + NATS drain
- **Kamera kalibrasyon**: YAML tabanlı, bbox → lat/lon projeksiyonu

### UI
- **deck.gl overlay**: track trails + confidence-graded renk
- **Viewshed panel**: sensor coverage sektör polygon (düz-Earth, DEM hazırlıkta)
- **WebSocket auto-reconnect**

### Altyapı
- **Promtail + Loki**: Docker log → JSON structured → Loki
- **NATS Prometheus exporter**: /metrics endpoint
- **Prometheus alerts**: 5 SLO kuralı (FPS, inference, fusion tick, gateway clients, multi-sensor drought)
- **FreeTAKServer**: self-build Dockerfile (public image yok)
- **12 Docker konteyner**

### Bridges
- **COP → NATS bridge**: eski simülatör yeni pipeline'a bağlanır
- **Replay publisher**: JSONL recordings → NATS (kayıttan oynat)

### Test + CI
- 623 → **833 test** (+210 yeni)
- 24 yeni servis import CI verification
- Dockerfile.services multi-service image

---

## [v4] 2026-04-19 — 20 Boşluk Audit Kapatması

### Yeni Servisler
- RF ODID parser (ASTM F3411, pure Python, no ctypes)
- RF live listener (scapy/bleak stub)
- WiFi OUI service (13 bilinen drone MAC prefix)
- Fusion service (NATS orchestrator)
- Track gateway (WebSocket hub)
- CoT workers (pytak QueueWorker pattern)
- CoT validator (yapısal + XSD)
- Autonomy intercept planner + geofence + MAVSDK bridge

### Altyapı
- Docker compose (postgres+pgvector, nats, redpanda, grafana, loki, tempo, prometheus, portainer)
- Grafana NIZAM klasörü + auto-provisioning + camera+fusion dashboard
- Prometheus scrape jobs (camera, fusion, gateway, rf)

### Karar Katmanı
- ThreatAssessment + weighted scoring (5 factor)
- ROE YAML loader + first-match evaluator
- Decision schema + Action enum (LOG/ALERT/ENGAGE/HANDOFF)
- ENGAGE varsayılan disabled + otomatik operatör onayı

---

## [v3] 2026-04-18 — Counter-UAS 8-Faz Scaffolding

### Faz 0-7 İskeletleri
- Faz 0: Docker altyapı
- Faz 1: YOLO kamera detector
- Faz 2: ODID Remote ID parser
- Faz 3: Kalman + Hungarian füzyon
- Faz 4: CesiumJS UI
- Faz 5: CoT XML builder
- Faz 6: Rule engine + ROE
- Faz 7: Geofence + intercept

- 96 yeni test (camera, RF, fusion, cot, decision, autonomy, gateway)
- AGENTS.md (89 vendor repo şablonu)
- 18-haftalık plan: docs/NIZAM_Envanter_ve_Plan.md

---

## [v2] 2026-04-14 — Railway Deploy Denemesi

- Dockerfile CPU-only torch + opencv-headless
- railway.toml + Procfile
- Railway başarısız (public deploy savunma projesi için uygun değil — iptal)

---

## [v1] 2026-04-10 → 2026-04-17 — COP Prototipi

- FastAPI server + 25 router + WebSocket fan-out
- 29 AI analyzer (anomaly, predictor, ROE, tactical, fusion, ml_threat, ...)
- ML modelleri: threat_rf (RandomForest 83.9% CV), trajectory (LSTM, OpenSky fine-tune 112m RMSE)
- 6 demo senaryo (swarm, coordinated, decoy, missile, single, multi-axis)
- AAR + replay + timeline + coordinated attack pattern
- SHA-256 hash-chained decision lineage
- TimescaleDB persistence
- 623 test

---

## Yol Haritası (v6+ yapılacak)

### Sıradaki sprint
- [ ] Drone-özel YOLO fine-tune (Roboflow dataset → Colab → yolov8n-drone.pt)
- [ ] Gerçek DJI ile saha testi (video kanıt)
- [ ] 72 saat kesintisiz kararlılık testi
- [ ] False positive baseline ölçümü
- [ ] TensorRT/ONNX optimizasyon (5→30 FPS hedef)
- [ ] Multi-kamera canlı test (2 webcam)
- [ ] LangGraph PostgreSQL checkpoint integration test

### Uzun vadeli
- [ ] DJI OcuSync decoder (GNU Radio + HackRF)
- [ ] PX4 SITL + Nav2 MPPI intercept simülasyonu
- [ ] DEM + maptalks viewshed (gerçek line-of-sight)
- [ ] NATO STANAG uyum sertifikasyon süreci
