# NIZAM — Counter-UAS Sistemi
## Envanter ve Geliştirme Planı

**Tarih:** 19 Nisan 2026
**Kapsanan Repo:** 89 (12 teknoloji alanı)
**Toplam Boyut:** 3.2 GB kaynak kod

---

## 1. KAPSAM VE AMAÇ

NIZAM, çok-sensörlü (görüntü + RF + radar) tespit, Kalman filtresi füzyon, TAK protokolü üzerinden taktik dağıtım ve opsiyonel otonom intercept yeteneklerini içeren bir **counter-UAS** sistemidir.

**Hedef yetenekler:**
- Pasif drone tespiti (kamera + Remote ID + DJI OcuSync + WiFi OUI)
- Multi-sensor Kalman füzyonu (kaybolan tespitlerde bile süreklilik)
- Gerçek zamanlı 3D taktik harita (operatör için)
- ATAK/WinTAK istemcilerine otomatik CoT dağıtımı
- Opsiyonel: Otonom karşı-drone (PX4 + MAVSDK + Nav2)
- AI destekli karar katmanı (tehdit sınıflandırma + ROE kontrolü)

---

## 2. TAM ENVANTER — 89 REPO / 12 ALAN

### ALAN 1: Computer Vision (8/8 ✅)

| Repo | Dil | Boyut | NIZAM Rolü |
|------|-----|-------|-----------|
| ultralytics | Python | 20MB | YOLO v8/v11 — ana tespit modeli |
| yolo_ros | Python | <1MB | YOLO → ROS 2 topic bridge |
| rf-detr | Python | <1MB | Roboflow DETR — transformer tespit |
| supervision | Python | 45MB | Tracker utilities, annotators |
| trackers | Python | <1MB | Roboflow modular trackers |
| ByteTrack | Python | 63MB | Yoğun MOT — kalabalık sahne |
| opencv-4.x | C++ | 96MB | Temel görüntü işleme |
| norfair | Python | 275MB | Custom distance tracker + ReID |

### ALAN 2: Robotics & ROS 2 (8/9)

| Repo | Dil | Boyut | NIZAM Rolü |
|------|-----|-------|-----------|
| navigation2 | C++ | 65MB | ROS 2 nav stack (MPPI, BT) |
| MAVSDK | C++/Py | 7MB | Drone kontrol API (follow_me, offboard) |
| mavlink | XML | <1MB | Protokol tanımları (ADSB_VEHICLE, FOLLOW_TARGET) |
| PX4-Autopilot | C++ | 271MB | Uçuş firmware + landing_target_estimator |
| rtabmap | C++ | ~20MB | 3D SLAM (LiDAR + RGB-D + loop closure) |
| isaac_ros_common | C++/Py | <5MB | NITROS (GPU zero-copy ROS 2) |
| isaac_ros_visual_slam | C++ | ~5MB | cuVSLAM — GPS-denied, 250fps |
| nice-slam | Python | 156MB | Neural implicit SLAM (offline ref) |
| ❌ LIO-SAM | — | — | Kritik değil (RTAB-Map kapsıyor) |

### ALAN 3: AI Agent Framework (8/8 ✅)

| Repo | Dil | Boyut | NIZAM Rolü |
|------|-----|-------|-----------|
| claude-agent-sdk-python | Python | <1MB | Claude entegrasyonu — karar motoru |
| langgraph | Python/JS | 15MB | State machine — NIZAM ana sinir sistemi |
| openai-agents-python | Python | 5MB | Guardrails + handoff tasarım deseni |
| autogen | Py/.NET | 24MB | Multi-agent chat, MagenticOne |
| semantic-kernel | 3 dil | 18MB | Multi-LLM adapter (Claude + Ollama) |
| crewAI | Python | 150MB | Rol tabanlı multi-agent |
| llama_index | Python | 259MB | RAG — doktrin/ROE bilgi tabanı |
| dify | Py/TS | 35MB | Visual LLM pipeline + operatör UI |

### ALAN 4: Local LLM Inference (9/9 ✅)

| Repo | Dil | Boyut | NIZAM Rolü |
|------|-----|-------|-----------|
| llama.cpp | C++ | 38MB | Temel — tüm stack'in çekirdeği |
| ollama | Go | 33MB | Kolay yerel deploy + Anthropic API |
| vLLM | Py+CUDA | 40MB | GPU cluster + Anthropic API + MCP |
| TGI | Rust+Py | 22MB | Multi-hardware (TensorRT, Gaudi, Neuron) |
| SGLang | Py+Triton | 30MB | Constrained/structured generation |
| LocalAI | Go | 13MB | OpenAI API drop-in, multi-backend |
| GPT4All | C++ | 15MB | Hava-gap laptop |
| KoboldCpp | C++ | 61MB | Tek binary — STT/TTS/vision/video |
| Jan | TS/Rust | 226MB | Masaüstü UI + RAG + vector-db extension |

### ALAN 5: Data Streaming (5/6)

| Repo | Dil | Boyut | NIZAM Rolü |
|------|-----|-------|-----------|
| nats-server | Go | 19MB | Edge bus — 20MB binary, LeafNode federation |
| redpanda | C++/Go | 40MB | Ana bus — Kafka API, JVM yok |
| automq | Go+C++ | 22MB | Cloud S3 Kafka (uzun dönem depolama) |
| flink | Java | 69MB | Stream processing (CEP, windowing) |
| pulsar | Java | 40MB | Multi-tenant (çoklu site) |
| ❌ Kafka | — | — | AutoMQ kapsıyor |

### ALAN 6: Vector Database (6/6 ✅)

| Repo | Dil | Boyut | NIZAM Rolü |
|------|-----|-------|-----------|
| faiss | C++/CUDA | 7MB | In-memory hızlı eşleştirme (RF imzası) |
| pgvector | C | <1MB | PostgreSQL extension (track metadata) |
| qdrant | Rust | 15MB | Production vector server + GPU |
| milvus | Go+C++ | 30MB | Büyük ölçek, cluster |
| chroma | Py+Rust | 23MB | LangGraph/LlamaIndex default |

### ALAN 7: Monitoring & Observability (8/10)

| Repo | Dil | Boyut | NIZAM Rolü |
|------|-----|-------|-----------|
| prometheus | Go | 27MB | Metrik standardı |
| grafana | Go+React | 30MB | Görselleştirme merkezi |
| loki | Go | 25MB | Log agregasyonu (LogQL) |
| tempo | Go | 20MB | Distributed trace |
| mimir | Go | 18MB | Scalable Prometheus |
| VictoriaMetrics | Go | 52MB | 21 uygulama — VM + victoria-logs hepsi bir arada |
| signoz | Go | 20MB | OpenTelemetry-native, Datadog alt. |
| openobserve | Rust | 18MB | Tek binary (log+metrik+trace+RUM) |
| ❌ Jaeger | — | — | Tempo kapsıyor |
| ❌ OTel Collector | — | — | SigNoz built-in |

### ALAN 8: Self-Hosted Deployment (7/7 ✅)

| Repo | Dil | Boyut | NIZAM Rolü |
|------|-----|-------|-----------|
| portainer | Go+React | 19MB | Docker/K8s yönetim UI |
| coolify | PHP (Laravel) | 18MB | Geliştirme PaaS, git push deploy |
| komodo | Rust+TS | 7MB | Merkez — periphery altyapı otomasyonu |
| kamal | Ruby | 2MB | SSH deploy + OTEL built-in |
| dokku | Bash | 3MB | Minimal edge PaaS |
| dokploy | TS | 35MB | Modern PaaS + monitoring built-in |
| caprover | TS | 14MB | Docker Swarm cluster |

### ALAN 9: Sensor Fusion & MOT (4/4 ✅)

| Repo | Dil | Boyut | NIZAM Rolü |
|------|-----|-------|-----------|
| filterpy | Python | 3MB | KF/EKF/UKF/IMM/PF — üretim kütüphanesi |
| Kalman-Bayesian-Filters book | Jupyter | 21MB | 14 bölüm referans kitap |
| smart_track | Python | <1MB | ROS 2 KF-guided fusion deseni |
| Sensor-Fusion-3D-MOT | Python | <1MB | LiDAR+Camera EKF + track mgmt |
| RAFT | C | <1MB | Radar fusion + CAN + RT referans |

### ALAN 10: TAK/CoT Ekosistemi (9/10)

| Repo | Dil | Boyut | NIZAM Rolü |
|------|-----|-------|-----------|
| pytak | Python | 2MB | TAK/CoT Python kütüphanesi |
| FreeTAKServer | Python | 1MB | Tam TAK sunucu |
| goatak | Go | 3MB | Go TAK sunucu (hafif production) |
| taky | Python | <1MB | Ultra-hafif TAK sunucu |
| adsbxcot | Python | 7MB | ⭐ Bridge deseni şablonu |
| stratuxcot | Python | 41MB | Yedek referans |
| cotproxy | Python | <1MB | Inline CoT transformer |
| ATAK_push_cots | Python | <1MB | Serverless push |
| ATAK-CIV | Java | 283MB | CoT XSD + plugin SDK |
| ❌ OpenTAKRouter | — | — | 9 TAK repo yeterli |

### ALAN 11: 3D GIS (10/10 ✅)

| Repo | Dil | Boyut | NIZAM Rolü |
|------|-----|-------|-----------|
| cesium | JS | 85MB | 3D globe — CZML streaming, terrain |
| maplibre-gl-js | TS+GLSL | 134MB | Vektör tile motor |
| maptalks.js | JS | 5MB | ⭐ ViewshedAnalysis + InSightAnalysis |
| deck.gl | TS | 64MB | WebGL layers (TripsLayer, ArcLayer) |
| kepler.gl | TS | 30MB | AI assistant + DuckDB analiz |
| terriajs | TS | 30MB | Katalog + Cesium platform |
| openlayers | JS | 20MB | Hafif 2D overhead |
| openmaptiles | YAML | <1MB | Çevrimdışı base map tile şeması |
| cesium-vector-provider | JS | 1MB | Cesium + MapLibre köprüsü |
| gdal | C++ | 46MB | Universal format çevirici (DTED, KML) |

### ALAN 12: SDR/RF/Counter-UAS (10/11)

| Repo | Dil | Boyut | NIZAM Rolü |
|------|-----|-------|-----------|
| gnuradio | C++/Py | 30MB | Flowgraph motoru |
| opendroneid-core-c | C | <1MB | ASTM F3411 Remote ID decoder |
| dji_droneid | C++/Matlab | <1MB | DJI OcuSync pasif decoder |
| receiver-android | Java | 3MB | Telefon → Remote ID sensörü |
| RF-Drone-Detection | Python | <1MB | WiFi OUI + HackRF SVM |
| SDR++ | C++ | 4MB | Modern SDR UI + 27 kaynak |
| gqrx | C++/Qt | 6MB | GNU Radio analiz UI |
| gnss-sdr | C++ | 15MB | GPS spoofing tespiti |
| TempestSDR | Java/C | 65MB | TEMPEST/ISR (GCS ekran okuma) |
| srsRAN | C++ | 10MB | 5G RAN (arşivlendi) |
| ❌ gr-droneid-update-3.10 | — | — | dji_droneid GNU Radio OOT — şart |

---

## 3. KAPSAMA ÖZETİ

| Alan | Tamamlanan | Yüzde |
|------|-----------|-------|
| 1 Computer Vision | 8/8 | 100% |
| 2 Robotics | 8/9 | 89% |
| 3 AI Agent | 8/8 | 100% |
| 4 Local LLM | 9/9 | 100% |
| 5 Streaming | 5/6 | 83% |
| 6 Vector DB | 6/6 | 100% |
| 7 Monitoring | 8/10 | 80% |
| 8 Deployment | 7/7 | 100% |
| 9 Sensor Fusion | 4/4 | 100% |
| 10 TAK/CoT | 9/10 | 90% |
| 11 3D GIS | 10/10 | 100% |
| 12 SDR/RF | 10/11 | 91% |
| **TOPLAM** | **92/94** | **97.9%** |

---

## 4. MİMARİ — KATMAN GÖRÜNÜMÜ

```
Operatör UI: CesiumJS 3D Globe — ATAK Tablet — Dify Visual — Kepler.gl Analiz
                          ↓ CoT / WebSocket / REST
TAK/CoT Dağıtım: FreeTAKServer — GoATAK — taky — cotproxy
                 pytak workers: droneid_to_cot, fusion_to_cot, mavlink_to_cot
                          ↓
AI Karar: LangGraph State Machine — Claude Agent SDK
          LlamaIndex RAG (doktrin) — CrewAI (rol takımı)
          SGLang constrained output — OpenAI Agents guardrails
                          ↓
Füzyon: filterpy (KF/EKF/UKF/IMM) — SMART-TRACK pattern
        ByteTrack — Norfair — Sensor-Fusion-3D-MOT track management
        FAISS/Qdrant (nearest neighbor - drone model tanımlama)
                          ↓
Veri Hattı: Edge Node (Jetson/RPi) → NATS JetStream → Merkez Node
                                     LeafNode bridge   Redpanda
                                                        ↓
                                                 Apache Flink (CEP)
                                                        ↓
                                                 AutoMQ on S3 (arşiv)
                          ↓
Sensör:
  Görüntü: Ultralytics YOLO → Supervision → ByteTrack
           RF-DETR (transformer alt.) — yolo_ros (ROS 2 bridge)
  RF/SDR:  GNU Radio + opendroneid-core-c (ASTM F3411 Remote ID)
           dji_droneid + gr-droneid-update-3.10
           receiver-android — RF-Drone-Detection — GNSS-SDR
  Derinlik: RealSense → RTAB-Map — Isaac vSLAM (GPS-denied, 250fps)
                          ↓
Otonom (Opsiyonel - intercept):
  PX4 Autopilot — MAVSDK (follow_me, offboard)
  Nav2 MPPI planner — uxrce_dds_client (ROS 2 → PX4)
```

---

## 5. VERİ AKIŞI — HEDEFİN YOLCULUĞU

```
t=0ms:   Sensör tespit (YOLO bbox / ODID GPS / BT drone_id)
t=2ms:   NATS JetStream yayını (nizam.raw.camera/rf/bt.{id})
t=5ms:   LeafNode → Redpanda (merkez)
t=10ms:  Flink CEP: 5s pencerede sensör eşleştirme
t=15ms:  Füzyon: filterpy.IMMEstimator + FAISS drone model tanıma
t=20ms:  Karar: LangGraph → Claude Agent SDK → ROE check → LOG/ALERT/ENGAGE
t=25ms:  CoT üretimi (pytak worker)
t=30ms:  cotproxy zenginleştirme (callsign, icon, threat_level)
t=35ms:  FreeTAKServer → ATAK istemcileri (TCP multicast CoT)
t=40ms:  CesiumJS entity update + deck.gl reposition
t=100ms+ (opsiyonel): MAVSDK.follow_target() → PX4
```

**Hedef end-to-end gecikme:** <100ms tespit → operatör ekranı
**Intercept başlatma gecikmesi:** <500ms tespit → motor komutu

---

## 6. KRİTİK BAĞIMLILIKLAR

### Yazılacak Entegrasyon Kodu (Kritik Yolda)

| Entegrasyon | Şablon | Yazılacak Dosya |
|-------------|--------|-----------------|
| ODID → CoT | adsbxcot | `services/cot/droneid_to_cot.py` |
| YOLO track → CoT | adsbxcot | `services/cot/fusion_to_cot.py` |
| MAVLink → CoT | adsbxcot | `services/cot/mavlink_to_cot.py` |
| Füzyon servis | SMART-TRACK + S-F-3D-MOT | `services/fusion/kf_fusion.py` |
| Decision graph | LangGraph examples | `services/decision/threat_graph.py` |
| ROE RAG | LlamaIndex | `services/knowledge/roe_rag.py` |
| Viewshed hesap (UI) | maptalks.analysis | `ui/src/coverage_map.js` |

---

## 7. FAZ PLANI — 18 HAFTA

### FAZ 0: Altyapı Kurulumu (Hafta 1-2)
- [ ] Docker + Docker Compose + Portainer
- [ ] PostgreSQL 16 + pgvector
- [ ] NATS JetStream (edge bus)
- [ ] Redpanda (ana bus)
- [ ] Prometheus + Grafana + VictoriaMetrics
- [ ] Tempo + Loki
- [ ] CI/CD pipeline

**Çıktı:** `docker compose up` ile tüm altyapı ayakta.

### FAZ 1: Tek Sensör Tespit MVP (Hafta 3-4)
- [ ] `services/detectors/camera/yolo_service.py`
- [ ] NATS `nizam.raw.camera.{id}` yayını
- [ ] Grafana: tespit akışı dashboard

**Çıktı:** Webcam + drone fotoğrafı → Grafana sayaç artıyor.

### FAZ 2: RF Tespit (Hafta 5-6)
- [ ] `services/detectors/rf/odid_service.py` (opendroneid-core-c ctypes)
- [ ] GNU Radio DJI OcuSync flowgraph
- [ ] `services/detectors/rf/wifi_oui_service.py`

**Çıktı:** DJI drone açıldığında hem kamera hem RF NATS'e düşüyor.

### FAZ 3: Multi-Sensor Füzyon (Hafta 7-9)
- [ ] `services/fusion/track_manager.py` (Sensor-Fusion-3D-MOT deseni)
- [ ] `services/fusion/kf_engine.py` (filterpy IMM)
- [ ] `services/fusion/association.py` (Hungarian)
- [ ] FAISS drone model lookup

**Çıktı:** Aynı drone → tek track ID, birleşmiş güven skoru.

### FAZ 4: 3D Görsel + Operatör UI (Hafta 10-11)
- [ ] CesiumJS 3D globe (CZML stream)
- [ ] deck.gl overlay
- [ ] maptalks.analysis: ViewshedAnalysis
- [ ] OpenMapTiles çevrimdışı base map

**Çıktı:** Operatör 3D haritada drone'u gerçek zamanlı görüyor.

### FAZ 5: TAK/CoT Dağıtım (Hafta 12-13)
- [ ] `services/cot/droneid_to_cot.py` (adsbxcot şablon)
- [ ] FreeTAKServer deploy + mTLS
- [ ] ATAK tablet test

**Çıktı:** Gerçek ATAK tablette tespit marker'ı görünüyor.

### FAZ 6: AI Karar Katmanı (Hafta 14-15)
- [ ] LangGraph state machine (Claude Agent SDK)
- [ ] LlamaIndex RAG (doktrin + ROE)
- [ ] SGLang constrained output şeması
- [ ] `services/decision/threat_graph.py`

**Çıktı:** Her track'e LOG/ALERT/ENGAGE/HANDOFF kararı atanıyor.

### FAZ 7: Otonom Intercept — Opsiyonel (Hafta 16-18)
- [ ] PX4 SITL (Gazebo)
- [ ] MAVSDK follow_me mission
- [ ] Nav2 MPPI planner

**Çıktı:** Simülasyonda karşı-drone tehdit drone'u takip ediyor.

---

## 8. KRİTİK RİSKLER

| Risk | Olasılık | Azaltma |
|------|----------|---------|
| gr-droneid-update-3.10 yazmak gerekebilir | Yüksek | dji_droneid docs + GNU Radio OOT tutorial |
| YOLO drone tespit doğruluğu düşük | Orta | Sahaya özel fine-tune dataset |
| Sensor saat senkronizasyonu | Yüksek | NTP+PTP, timestamp veri akışının içinde |
| LLM karar hallucination | Orta | SGLang constrained schema + guardrails |
| PX4 SITL gerçeklik farkı | Yüksek | HITL (hardware-in-the-loop) test şart |

---

## 9. ÖLÇÜLEBILIR BAŞARI KRİTERLERİ

| Metrik | Hedef |
|--------|-------|
| Sensör tespit → operatör ekranı | <100ms P95 |
| Sensör tespit → intercept komutu | <500ms P95 |
| Tespit hassasiyet (known drone) | >95% |
| False positive oranı | <2/saat |
| Sistem uptime | >99.5% |
| ROE ihlali | 0 |

---

*Belge sürümü: v1.0 — İlk plan taslağı*
*Sonraki güncelleme: Faz 0 sonunda (hafta 2)*
