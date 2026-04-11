# NIZAM COP

[![CI](https://github.com/altunbulakemre75/nizam-cop/actions/workflows/ci.yml/badge.svg)](https://github.com/altunbulakemre75/nizam-cop/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-623%20passed-brightgreen)](https://github.com/altunbulakemre75/nizam-cop/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Real-Time Command & Control (C2) / Common Operational Picture (COP) System**

NIZAM is an open-source C2/COP prototype inspired by Anduril Lattice and Palantir Gotham. It demonstrates the core architectural concepts used in modern command & control, ISR, and aerospace ground-segment software: event-driven sensor fusion, multi-layer AI decision support, cryptographically-linked decision provenance, LSTM trajectory prediction, and operator-centric situational awareness.

Runs fully offline. No cloud services required.

---

## What NIZAM Does

**Ingests** real and simulated sensor events (radar, RF, ADS-B, AIS, electro-optical, generic REST) through an asynchronous agent pipeline.

**Fuses** multi-sensor observations into unified tracks with intent classification and ML threat scoring (RandomForest, 94%+ accuracy on synthetic workloads).

**Predicts** future trajectories using a trained LSTM network (12 steps ahead per track), drawn live on the map alongside Kalman-filter short-term estimates.

**Reasons** using a layered AI stack: anomaly detection, swarm/coordinated-attack pattern recognition, tactical recommendations, and rules-of-engagement advisories.

**Records** every AI decision in a SHA-256 hash-chained lineage store. Each record carries `hash` and `prev_hash` fields — modifying or deleting any record breaks the chain, detectable by `verify_chain()`.

**Persists** time-series track and threat events to TimescaleDB hypertables (composite primary key `(id, time)` — partition-aware). Zones, assets, tasks, and waypoints survive server restarts.

**Synchronises** state across multiple COP nodes via delta-push replication with vector clock conflict resolution and split-brain detection.

**Broadcasts** live state to a tabbed Leaflet browser UI over WebSocket with multi-operator support: track claiming, operator presence, TTS voice alerts, and shared task queue.

---

## Capabilities

### Core COP

- Real-time WebSocket state distribution — tracks, threats, zones, assets, tasks, waypoints
- Multi-sensor fusion: radar + RF bearing + EO/camera, with supporting-sensor provenance
- Intent classification: attack / reconnaissance / loitering / unknown
- ML threat scoring (RandomForest) with rule-based fallback
- Zone system: restricted / kill / friendly polygons with ray-casting breach detection
- Autonomous task proposal (ENGAGE / OBSERVE) with operator approve / reject workflow
- Fire control loop: approve ENGAGE → effector impact animation → track removal
- TTS voice alerts with alarm-fatigue prevention (batch window, dedup, priority queue)
- Altitude colour mode — track markers colour-coded by altitude band (green → purple)
- Pause / resume, reset, JSONL replay with time-slider
- Multi-operator sessions: track claiming, presence indicators, conflict prevention (409)
- Multi-node federation: delta-push sync, vector clocks, split-brain detection, conflict log

### AI Decision Support

- **LSTM trajectory prediction** — 12-step ahead waypoints per track, drawn as dashed polylines color-coded by threat level
- Kalman-filter short-term prediction (60 s, 12 future points) + uncertainty cones
- Anomaly detection: speed spikes, heading reversals, intent shifts
- Swarm detection: proximity + correlated heading clustering
- Coordinated attack detection: pincer, convergence, zone-targeted, asset-targeted
- Predictive zone breach detection
- Tactical recommendation engine (intercept, zone warning, escalate, withdraw, monitor, reposition)
- Rules-of-Engagement (ROE) advisory (WEAPONS_FREE / WEAPONS_TIGHT / WEAPONS_HOLD / TRACK_ONLY)
- After-Action Report (AAR) generator
- LLM operator advisor (Claude / OpenAI API, rule-based fallback when no key set)

### Decision Lineage — SHA-256 Hash Chain

Every track carries a full decision provenance chain. Right-click any marker → **Decision Lineage** modal shows every reasoning step with hash integrity status:

```
T-R012-A018  →  HIGH (0.94)
├─ ingest        hash=73c17c3d...  radar-01 detected range=1200m, az=185°
├─ threat_assess hash=e7bba53b...  HIGH score=92, intent=attack
├─ ml_threat     hash=d98925fa...  RandomForest → HIGH (0.94)
├─ anomaly       hash=a1b2c3d4...  INTENT_SHIFT (CRITICAL) loitering→attack
├─ coord_attack  hash=f4e3d2c1...  PINCER (CRITICAL) 8 tracks, 23s to convergence
├─ tactical      hash=9e8f7a6b...  ESCALATE P1 — SWARM DETECTED
├─ roe           hash=5c4b3a29...  WEAPONS_TIGHT (HIGH urgency)
└─ task_proposer hash=1d2e3f40...  ENGAGE proposed, awaiting operator approval
```

Each record contains:
- `hash` — SHA-256 of record content
- `prev_hash` — pointer to previous record (`"000...0"` for genesis)
- `decision_id`, `timestamp`, `stage`, `summary`, `inputs`, `outputs`, `rule`

`verify_chain(track_id)` walks the full chain and returns `{"valid": bool, "broken_at": index}`.

### Real Sensor Adapters

| Adapter | Protocol | Use Case |
|---|---|---|
| `adapters/adsb_adapter.py` | dump1090 JSON / Beast | Live aircraft via RTL-SDR |
| `adapters/ais_adapter.py` | NMEA-0183 serial / TCP | Maritime vessel tracking |
| `adapters/rest_adapter.py` | Generic HTTP REST poll | Any sensor with a JSON API |
| `adapters/mqtt_adapter.py` | MQTT v5 (paho-mqtt) | IoT sensors, radar feeds, telemetry |

### Platform

- PostgreSQL / TimescaleDB persistence — hypertables for `track_events`, `threat_events`, `alert_records`
- JWT authentication with ADMIN / OPERATOR / VIEWER roles (enabled via `AUTH_ENABLED=true`)
- Docker + Docker Compose (one command: `docker compose up --build`)
- Production setup script (`scripts/prod_setup.sh`): TLS cert generation, secret rotation, Docker boot
- GitHub Actions CI: 623 pytest tests + end-to-end smoke test
- Runtime metrics endpoint (`/api/metrics`): ingest rate, tactical p50/p95, WS fan-out
- Tactical engine benchmark (`scripts/bench_tactical.py`): automated p50/p95/p99 measurement with per-module breakdown

---

## System Architecture

```mermaid
flowchart LR
    subgraph SIM["Simulated Sensors"]
        WORLD[world_agent<br/>truth source]
        RADAR[radar_sim]
        RF[rf_sim]
        EO[eo_sim]
        WORLD --> RADAR
        WORLD --> RF
        WORLD --> EO
    end

    subgraph REAL["Real Sensor Adapters"]
        ADSB[ADS-B<br/>dump1090]
        AIS[AIS<br/>NMEA-0183]
        REST[REST<br/>HTTP poll]
        MQTT[MQTT<br/>paho-mqtt]
    end

    FUSER[fuser<br/>multi-sensor fusion]
    PUB[cop_publisher<br/>bounded queue]

    RADAR --> FUSER
    RF --> FUSER
    EO --> FUSER
    FUSER --> PUB

    subgraph COP["COP Server (FastAPI)"]
        INGEST["/ingest"]
        AI["AI Engine<br/>(thread pool)"]
        LINEAGE[SHA-256<br/>Hash Chain]
        FC[Fire Control]
        INGEST --> AI
        AI --> LINEAGE
        AI --> FC
    end

    PUB --> INGEST
    ADSB --> INGEST
    AIS --> INGEST
    REST --> INGEST
    MQTT --> INGEST

    subgraph AILAYER["AI Decision Support"]
        ML[ML Threat<br/>RandomForest]
        LSTM[LSTM<br/>Trajectory]
        KAL[Kalman<br/>Predictor]
        ANOM[Anomaly<br/>Detection]
        COORD[Coordinated<br/>Attack]
        TAC[Tactical<br/>Recommender]
        ROE[ROE<br/>Advisor]
    end

    AI --> ML
    AI --> LSTM
    AI --> KAL
    AI --> ANOM
    AI --> COORD
    AI --> TAC
    AI --> ROE

    DB[(TimescaleDB<br/>hypertables)]
    AI --> DB

    subgraph UI["Browser UI (Leaflet)"]
        MAP[Track Map +<br/>LSTM polylines]
        TABS[Tab Panels]
        CHAIN[Lineage Modal]
        OPS[Multi-operator<br/>presence]
    end

    COP -.->|WebSocket| UI

    subgraph FED["Federation"]
        PEER1[COP Node 2]
        PEER2[COP Node 3]
    end

    COP <-.->|delta-push<br/>vector clocks| FED

    classDef sensor fill:#1e3a5f,stroke:#4a90e2,color:#fff
    classDef ai fill:#4a1e5f,stroke:#a04ae2,color:#fff
    classDef store fill:#1e5f3a,stroke:#4ae290,color:#fff
    class RADAR,RF,EO,WORLD,ADSB,AIS,REST sensor
    class ML,LSTM,KAL,ANOM,COORD,TAC,ROE ai
    class DB store
```

**Key design properties:**

- **Event loop never stalls.** The AI engine runs in `run_in_executor` thread pool. `/ingest` returns in milliseconds even during 2-second AI passes.
- **Back-pressure proof.** `cop_publisher` uses a bounded queue with drop-oldest eviction.
- **Tamper-evident lineage.** SHA-256 hash chain per track — any modification is detectable.
- **TimescaleDB native.** `(id, time)` composite primary key enables true time-series partitioning.

---

## Repository Layout

```
adapters/         real-world sensor adapters (ADS-B, AIS, REST, MQTT)
agents/           sensor simulation + fusion + cop_publisher
ai/
  _fast_math.py           numpy-vectorised geospatial helpers (pairwise dist, heading diff)
  anomaly.py              anomaly + swarm detection
  coordinated_attack.py   pincer / convergence / zone-targeted / asset-targeted detection
  lineage.py              SHA-256 hash-chained decision provenance
  llm_advisor.py          Claude / OpenAI operator advisor
  ml_threat.py            RandomForest threat classifier
  predictor.py            Kalman track prediction
  roe.py                  rules-of-engagement advisory
  tactical.py             recommendation engine
  timeline.py             threat score timeline
  trajectory.py           LSTM 12-step trajectory predictor
  trajectory_model.pt     trained LSTM weights (829 KB)
  zone_breach.py          predictive breach + uncertainty cones
  aar.py                  after-action report generator
auth/             JWT + role-based access
cop/
  server.py               FastAPI COP server (parallel AI engine, fire control, sync)
  sync.py                 multi-node federation (delta-push, vector clocks, split-brain)
  static/app.js           Leaflet UI (TTS alerts, altitude mode, nodes panel)
db/               SQLAlchemy + TimescaleDB models + migrations
k8s/              Kubernetes manifests
orchestrator/     agent registry + heartbeat
scenarios/        single_drone / swarm / coordinated / multi_axis_attack / decoy
scripts/
  bench_tactical.py       tactical engine latency benchmark (p50/p95/p99 + module breakdown)
  compare_scenarios.py    multi-scenario AAR comparison runner
  cot_test_sender.py      CoT/ATAK UDP multicast simulator
  load_test.py            1000-track throughput + latency test
  prod_setup.sh           TLS cert + secret generation + Docker boot
  smoke_test.py           end-to-end smoke test
tests/            623 pytest tests
train_trajectory.py  synthetic data generator + LSTM training script
start.py          one-command boot: orchestrator + COP + pipeline
```

---

## Quick Start

```bash
pip install -r requirements.txt

# Optional: train LSTM trajectory model (pre-trained weights included)
python train_trajectory.py --epochs 40 --samples 10000

# All-in-one boot:
python start.py --scenario scenarios/multi_axis_attack.json
```

Open **http://127.0.0.1:8100**

**Left-click** a track → threat timeline  
**Right-click** a track → Decision Lineage (hash chain) or Claim Track

### With PostgreSQL / TimescaleDB

```bash
# Start DB container
docker compose up db -d

# Set connection string
export DATABASE_URL=postgresql+asyncpg://nizam:nizam@localhost:5432/nizam

python start.py --scenario scenarios/multi_axis_attack.json
```

Tables are created automatically on first run. TimescaleDB hypertables are enabled if the extension is present.

### Key Endpoints

| URL | Purpose |
|---|---|
| `http://127.0.0.1:8100` | COP UI |
| `/api/metrics` | Runtime metrics (ingest rate, tactical p50/p95, WS) |
| `/api/ai/lineage/{track_id}` | Hash-chained decision provenance for a track |
| `/api/ai/status` | AI subsystem status (LSTM ready, ML model, LLM) |
| `/api/ai/aar` | After-action report |
| `/api/operators` | Active operator sessions |
| `/api/sync/peers` | Federation peer management (add/remove/list) |
| `/api/sync/conflicts` | Vector clock conflict log (GET/DELETE) |
| `http://127.0.0.1:8200` | Orchestrator agent health |

---

## LSTM Trajectory Training

Pre-trained weights (`ai/trajectory_model.pt`) are included. To retrain:

```bash
python train_trajectory.py --epochs 40 --samples 10000
# epoch  40/40  val=0.00010  (~50m RMSE)
```

Synthetic trajectory patterns: straight, constant-rate turn, acceleration/deceleration, S-curve, evasive jink. Model input: 20-step history × 5 features (Δx, Δy, speed, sin/cos heading). Output: 12-step predicted (Δx, Δy) offsets.

---

## Decision Lineage API

```bash
curl http://127.0.0.1:8100/api/ai/lineage/T-R012-A018
```

```json
{
  "track_id": "T-R012-A018",
  "summary": {"count": 8, "stages": ["anomaly","coord_attack","ingest","ml_threat","roe","tactical"], "first": "...", "last": "..."},
  "chain": [
    {
      "stage": "ingest",
      "summary": "Track update — sensors: radar-01, rf-01",
      "hash": "73c17c3d1bbf3d23...",
      "prev_hash": "0000000000000000...",
      "timestamp": "2026-04-09T07:14:22Z"
    },
    {
      "stage": "ml_threat",
      "summary": "RandomForest → HIGH (0.94)",
      "hash": "e7bba53b4b30acf6...",
      "prev_hash": "73c17c3d1bbf3d23...",
      "timestamp": "2026-04-09T07:14:23Z"
    }
  ]
}
```

---

## Performance

Load tested against 150+ concurrent tracks:

| Metric | v1 | v2 (parallel) | v3 (vectorised) |
|---|---|---|---|
| tactical.p50 | 1100 ms | ~120 ms | < 80 ms |
| tactical.p95 | 1920 ms | ~250 ms | < 150 ms |
| coord_attack (dominant module) | — | ~1285 ms | < 50 ms |
| tactical.failed | 0 | 0 | 0 |
| ingest failed | 0 | 0 | 0 |
| LSTM inference (per track) | < 5 ms | < 5 ms | < 5 ms |
| tactical interval | 3.0 s | 1.0 s | 1.0 s |
| operational latency | ~4.1 s | ~1.1 s | ~1.1 s |

**v2 optimisations:** 7 sub-modules run in parallel (`ThreadPoolExecutor`),
tactical interval reduced from 3 s → 1 s.

**v3 optimisations:** numpy-vectorised `O(N²)` pairwise distance/heading
matrices replace Python loops in swarm and coordinated-attack detection;
zone/asset approach checks batched into single `nearest_polygon_distances()`
calls (eliminates `O(zones × tracks × steps × vertices)` inner loop);
HIGH/MEDIUM threat-level pre-filter reduces working set before expensive
convergence analysis.

Run the benchmark yourself:

```bash
python scripts/bench_tactical.py --tracks 150 --warmup 10 --samples 30
```

---

## Running Tests

```bash
pytest tests/ -v                             # 623 unit tests
python scripts/smoke_test.py --duration 12   # end-to-end
python scripts/bench_tactical.py --tracks 150  # tactical engine latency
```

CI runs on every push to `main` (GitHub Actions, Python 3.10–3.13).

---

## Scenario Comparison & Load Tests

### Multi-scenario AAR comparison

Run several scenarios sequentially against a live COP server, then print a side-by-side AAR comparison and save the full reports:

```bash
# COP server must be running first (python start.py ...)
python scripts/compare_scenarios.py \
    --cop_url http://127.0.0.1:8100 \
    --duration 60 \
    --scenarios scenarios/single_drone.json \
                scenarios/swarm_attack.json \
                scenarios/coordinated_attack.json \
                scenarios/multi_axis_attack.json \
                scenarios/decoy_attack.json
```

For each scenario the runner: resets COP state → spawns the sensor pipeline → waits for it to exit → fetches `/api/ai/aar` → extracts key metrics (peak threat, coordinated attacks, zone breaches, tasks, risk level). Output is a comparison table on stdout plus a full JSON dump under `reports/comparison_<timestamp>.json`.

### 1000-track load test

```bash
# COP server must be running first
python scripts/load_test.py --tracks 1000 --duration 30 --rate_hz 2 --workers 32
```

Measures throughput, p50/p95/p99 latency, and error rate against `/ingest`. Exits non-zero if error rate exceeds 5%.

### Tactical engine benchmark

```bash
python scripts/bench_tactical.py --tracks 150 --warmup 10 --samples 30
```

Injects N tracks at a configurable rate, waits for the tactical engine to accumulate samples, then reports p50/p95/p99 and per-module breakdown from `/api/metrics`.

### CoT/ATAK interop test

```bash
python scripts/cot_test_sender.py --scenario swarm --tracks 8 --duration 30
```

Sends Cursor-on-Target SA messages over UDP multicast, simulating ATAK devices.

---

## Multi-Node Federation

NIZAM supports multi-node state synchronisation without a shared database. Each node pushes delta snapshots to registered peers. Conflict resolution uses vector clocks:

- **Incoming dominates local** → accept (clean update)
- **Local dominates incoming** → skip (stale)
- **Concurrent (split-brain)** → ephemeral data (tracks, threats) uses LWW by server_time; operator data (zones, assets, tasks, waypoints) is accepted + logged to `/api/sync/conflicts` for operator review

Split-brain detection triggers when a peer is unreachable for > 30 s. On reconnection, records modified during the partition window are flagged for audit.

```bash
# On node 1:
COP_NODE_ID=cop-node-01 python start.py --port 8100

# On node 2:
COP_NODE_ID=cop-node-02 python start.py --port 8200

# Register peers:
curl -X POST http://localhost:8100/api/sync/peers \
     -H "Content-Type: application/json" \
     -d '{"url": "http://localhost:8200"}'
```

---

## Scope and Limitations

Technical prototype for demonstration and educational purposes. Does **not** represent an active or deployed military system.

**In scope:** architecture, real-time behavior, AI decision support, multi-sensor fusion, cryptographically-linked decision provenance, LSTM trajectory prediction, TimescaleDB persistence, multi-operator coordination, multi-node federation.

**Out of scope:** classified data handling, fielded-grade security, production key management, live effector integration.

---

## Author

**Emre Altunbulak** — Mechanical Engineer

Focus areas: Command & Control Systems, Real-Time Operational Software, COP / ISR Architectures, AI Decision Support.

---

## Keywords

Common Operational Picture · C2 · ISR · Defense Software · Real-Time Systems · Event-Driven Architecture · Multi-Sensor Fusion · AI Decision Support · Decision Lineage · SHA-256 Hash Chain · LSTM Trajectory Prediction · TimescaleDB · Multi-Operator · Multi-Node Federation · Vector Clocks · CoT/ATAK · MQTT · Anduril Lattice · Palantir Gotham · FastAPI · WebSocket · Leaflet
