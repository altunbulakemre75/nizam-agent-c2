# NIZAM COP

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

**Broadcasts** live state to a tabbed Leaflet browser UI over WebSocket with multi-operator support: track claiming, operator presence, and shared task queue.

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
- Pause / resume, reset, JSONL replay with time-slider
- Multi-operator sessions: track claiming, presence indicators, conflict prevention (409)

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

### Platform

- PostgreSQL / TimescaleDB persistence — hypertables for `track_events`, `threat_events`, `alert_records`
- JWT authentication with ADMIN / OPERATOR / VIEWER roles (enabled via `AUTH_ENABLED=true`)
- Docker + Docker Compose (one command: `docker compose up --build`)
- GitHub Actions CI: 184 pytest tests + end-to-end smoke test
- Runtime metrics endpoint (`/api/metrics`): ingest rate, tactical p50/p95, WS fan-out

---

## System Architecture

```
                 ┌─ radar_sim ──┐
  world_agent ──┼─  rf_sim    ──┼─► fuser ─► cop_publisher ─► COP /ingest
                 └─  eo_sim   ──┘                                   │
                                                                     ▼
  Real sensors:                                          ┌─────────────────────┐
    ADS-B adapter ─────────────────────────────────────►│     COP Server      │
    AIS adapter   ─────────────────────────────────────►│     (FastAPI)       │
    REST adapter  ─────────────────────────────────────►│                     │
                                                         │  ├─ ML Threat      │
                                                         │  ├─ LSTM Traj.     │
                                                         │  ├─ Tactical       │
                                                         │  ├─ ROE            │
                                                         │  ├─ Anomaly        │
                                                         │  ├─ Fire Control   │
                                                         │  └─ Lineage (SHA)  │
                                                         └────────┬────────────┘
                                                                  │ WebSocket
                                                         ┌────────▼────────────┐
                                                         │    TimescaleDB      │
                                                         │  track_events       │
                                                         │  threat_events      │
                                                         │  alert_records      │
                                                         └─────────────────────┘
                                                                  │
                                                         ┌────────▼────────────┐
                                                         │    Browser UI       │
                                                         │  ├─ Track map       │
                                                         │  ├─ LSTM lines      │
                                                         │  ├─ Lineage chain   │
                                                         │  ├─ Tab panels      │
                                                         │  └─ Multi-operator  │
                                                         └─────────────────────┘
```

**Key design properties:**

- **Event loop never stalls.** The AI engine runs in `run_in_executor` thread pool. `/ingest` returns in milliseconds even during 2-second AI passes.
- **Back-pressure proof.** `cop_publisher` uses a bounded queue with drop-oldest eviction.
- **Tamper-evident lineage.** SHA-256 hash chain per track — any modification is detectable.
- **TimescaleDB native.** `(id, time)` composite primary key enables true time-series partitioning.

---

## Repository Layout

```
adapters/         real-world sensor adapters (ADS-B, AIS, REST)
agents/           sensor simulation + fusion + cop_publisher
ai/
  anomaly.py              anomaly + swarm detection
  coordinated_attack.py   pincer / convergence detection
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
cop/              FastAPI COP server + Leaflet UI
db/               SQLAlchemy + TimescaleDB models + migrations
k8s/              Kubernetes manifests
orchestrator/     agent registry + heartbeat
scenarios/        single_drone / swarm / coordinated / multi_axis_attack / decoy
tests/            184 pytest tests
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

Load tested against `multi_axis_attack` (67+ concurrent tracks):

| Metric | Value |
|---|---|
| tactical.p50 | 1100 ms |
| tactical.p95 | 1920 ms |
| tactical.failed | 0 |
| ingest failed | 0 |
| LSTM inference (per track) | < 5 ms (CPU) |

Tactical engine runs in executor thread pool — zero impact on `/ingest` latency.

---

## Running Tests

```bash
pytest tests/ -v                             # 184 unit tests
python scripts/smoke_test.py --duration 12   # end-to-end
```

CI runs on every push to `main` (GitHub Actions, Python 3.10–3.12).

---

## Scope and Limitations

Technical prototype for demonstration and educational purposes. Does **not** represent an active or deployed military system.

**In scope:** architecture, real-time behavior, AI decision support, multi-sensor fusion, cryptographically-linked decision provenance, LSTM trajectory prediction, TimescaleDB persistence, multi-operator coordination.

**Out of scope:** classified data handling, fielded-grade security, production key management, live effector integration.

---

## Author

**Emre Altunbulak** — Mechanical Engineer

Focus areas: Command & Control Systems, Real-Time Operational Software, COP / ISR Architectures, AI Decision Support.

---

## Keywords

Common Operational Picture · C2 · ISR · Defense Software · Real-Time Systems · Event-Driven Architecture · Multi-Sensor Fusion · AI Decision Support · Decision Lineage · SHA-256 Hash Chain · LSTM Trajectory Prediction · TimescaleDB · Multi-Operator · Anduril Lattice · Palantir Gotham · FastAPI · WebSocket · Leaflet
