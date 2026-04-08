# NIZAM

**Real-Time Command & Control (C2) / Common Operational Picture (COP) System**

NIZAM is an open-source, production-grade C2/COP prototype inspired by Anduril Lattice and Palantir Gotham. It demonstrates the core architectural concepts used in modern command & control, ISR, and aerospace ground-segment software: event-driven data flow, multi-sensor fusion, AI-assisted decision support, decision lineage, and operator-centric situational awareness.

No external services are required to run it end-to-end.

---

## What NIZAM Does

**Ingests** real and simulated sensor events (radar, RF, ADS-B, AIS, electro-optical, generic REST) through an asynchronous agent pipeline.

**Fuses** multi-sensor observations into unified tracks with intent classification and ML threat scoring (RandomForest, 94%+ accuracy).

**Reasons** using a layered AI stack: Kalman trajectory prediction, anomaly detection, swarm/coordinated-attack pattern recognition, tactical recommendations, and rules-of-engagement advisories.

**Records** every AI decision — from raw sensor hit to ENGAGE order — in a cryptographically-linked lineage chain that answers *"why is this track a threat?"* with full provenance.

**Broadcasts** live state to a Leaflet browser UI over WebSocket. Operators see tracks, threat levels, predicted trajectories, swarm patterns, fire control, and the complete decision chain for any track.

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

### AI Decision Support

- Kalman-filter trajectory prediction (60 s ahead, 12 future points per track)
- Anomaly detection: speed spikes, heading reversals, intent shifts
- Swarm detection: proximity + correlated heading clustering
- Coordinated attack detection: pincer, convergence, zone-targeted, asset-targeted
- Predictive zone breach + uncertainty cones
- Tactical recommendation engine (intercept, zone warning, escalate, withdraw, monitor, reposition)
- Rules-of-Engagement (ROE) advisory (WEAPONS_FREE / WEAPONS_TIGHT / WEAPONS_HOLD / TRACK_ONLY)
- After-Action Report (AAR) generator
- LLM operator advisor (Claude / OpenAI API, rule-based fallback)

### Decision Lineage (Palantir-inspired)

Every track carries a full decision provenance chain. Right-click any marker on the map → **Decision Lineage** modal shows every reasoning step:

```
T-R012-A018  →  HIGH (0.94)
├─ ingest        radar-01 detected range=1200m, az=185° @ T-00:42
├─ threat_assess HIGH score=92, intent=attack @ T-00:41
├─ ml_threat     RandomForest → HIGH (0.94) @ T-00:38
│                features: speed=32, closing=28, alt=150, intent_conf=0.87
├─ anomaly       INTENT_SHIFT (CRITICAL) loitering→attack @ T-00:36
├─ coord_attack  PINCER (CRITICAL) 8 tracks, 23s to convergence @ T-00:34
├─ tactical      ESCALATE P1 — SWARM DETECTED @ T-00:33
├─ roe           WEAPONS_TIGHT (HIGH urgency) @ T-00:31
└─ task_proposer ENGAGE proposed, awaiting operator approval @ T-00:30
```

Stages: `ingest → threat_assess → ml_threat → anomaly → coord_attack → tactical → roe → task_proposer → fire_control`

### Real Sensor Adapters

| Adapter | Protocol | Use Case |
|---|---|---|
| `adapters/adsb_adapter.py` | dump1090 JSON / Beast | Live aircraft via RTL-SDR |
| `adapters/ais_adapter.py` | NMEA-0183 serial / TCP | Maritime vessel tracking |
| `adapters/rest_adapter.py` | Generic HTTP REST poll | Any sensor with a JSON API |

### Platform

- PostgreSQL / TimescaleDB persistence (optional)
- JWT authentication with operator roles (optional)
- Docker + Docker Compose
- Kubernetes manifests (namespace, StatefulSet, HPA, Ingress)
- GitHub Actions CI: 184 pytest tests + end-to-end smoke test
- Runtime metrics endpoint (`/api/metrics`): ingest rate, tactical p50/p95, WS fan-out
- Scenario system: 5 built-in scenarios, fully configurable JSON

---

## System Architecture

```
                 ┌─ radar_sim ──┐
  world_agent ──┼─  rf_sim    ──┼─► fuser ─► cop_publisher ─► COP /ingest
                 └─  eo_sim   ──┘                                   │
                                                                     ▼
  Real sensors:                                          ┌───────────────────┐
    ADS-B adapter ──────────────────────────────────────►│    COP Server     │
    AIS adapter   ──────────────────────────────────────►│    (FastAPI)      │
    REST adapter  ──────────────────────────────────────►│                   │
                                                         │  STATE_LOCK       │
                                                         │  ├─ ML Threat     │
                                                         │  ├─ Tactical      │
                                                         │  ├─ ROE           │
                                                         │  ├─ Anomaly       │
                                                         │  ├─ Fire Control  │
                                                         │  └─ Lineage Store │
                                                         └────────┬──────────┘
                                                                  │ WebSocket
                                                                  ▼
                                                         ┌────────────────┐
                                                         │  Browser UI    │
                                                         │  (Leaflet.js)  │
                                                         │  ├─ Track map  │
                                                         │  ├─ Threat ML  │
                                                         │  ├─ Lineage ←  │
                                                         │  ├─ Task queue │
                                                         │  └─ AAR report │
                                                         └────────────────┘
```

**Key design properties:**

- **Back-pressure proof.** `cop_publisher` uses a bounded queue with drop-oldest eviction + worker thread pool. The pipeline reader never blocks on HTTP.
- **Event loop never stalls.** The AI engine (swarm, coord-attack, ML, ROE, lineage) runs in a `run_in_executor` thread pool. `/ingest` returns in milliseconds even during 2-second AI passes.
- **Shallow state snapshots** are handed to the executor — no torn reads under concurrent ingest.
- **Lineage is append-only.** Each subsystem writes its decision fragment; the chain is never mutated, making it audit-safe.

---

## Repository Layout

```
adapters/         real-world sensor adapters (ADS-B, AIS, REST)
agents/           sensor simulation + fusion + cop_publisher
  cop_publisher.py        pipeline → COP REST ingest (thread pool)
  fuser/                  multi-sensor fusion + intent + ML scoring
  radar_sim/ eo_sim/ rf_sim/ world/
ai/               AI decision support
  anomaly.py              anomaly + swarm detection
  coordinated_attack.py   pincer / convergence detection
  lineage.py              decision provenance store           ← NEW
  llm_advisor.py          Claude / OpenAI operator advisor
  ml_threat.py            RandomForest threat classifier
  predictor.py            Kalman track prediction
  roe.py                  rules-of-engagement advisory
  tactical.py             recommendation engine
  timeline.py             threat score timeline
  zone_breach.py          predictive breach + uncertainty cones
  aar.py                  after-action report generator
auth/             JWT + role-based access (optional)
cop/              FastAPI COP server
  server.py               ingest, state, WS, AI hooks, metrics, lineage API
  static/app.js           Leaflet UI + lineage modal + fire control
db/               SQLAlchemy + TimescaleDB
k8s/              Kubernetes manifests
orchestrator/     agent registry + heartbeat
scenarios/        single_drone / swarm / coordinated / multi_axis_attack / decoy
scripts/          compare_scenarios.py, smoke_test.py
tests/            184 pytest tests
run_pipeline.py   pipeline launcher
start.py          one-command boot: orchestrator + COP + pipeline
```

---

## Quick Start

```bash
pip install -r requirements.txt

# All-in-one (orchestrator + COP server + pipeline):
python start.py --scenario scenarios/multi_axis_attack.json
```

Open **http://127.0.0.1:8100** — the live Leaflet COP UI.

**Left-click** a track → threat timeline chart  
**Right-click** a track → Decision Lineage modal (full provenance chain)

### Key Endpoints

| URL | Purpose |
|---|---|
| `http://127.0.0.1:8100` | COP UI |
| `/api/metrics` | Runtime metrics (ingest rate, tactical p50/p95, WS) |
| `/api/ai/lineage/{track_id}` | Decision chain for a track (JSON) |
| `/api/ai/status` | AI subsystem status |
| `/api/ai/aar` | After-action report |
| `http://127.0.0.1:8200` | Orchestrator agent health |

### Benchmark Runner

```bash
python scripts/compare_scenarios.py --duration 30
```

Runs all 5 scenarios against a live COP server, prints a comparison table, saves full AAR bundle to `reports/`.

### Smoke Test

```bash
python scripts/smoke_test.py --duration 12
```

Boots server, runs pipeline, asserts metrics, exits 0 on pass.

---

## Performance

Load tested against `multi_axis_attack` (5 simultaneous drones/helicopters from 4 cardinal directions — 67+ concurrent tracks):

| Fix | Before | After |
|---|---|---|
| `cop_publisher` thread pool + drop-oldest queue | pipeline deadlocked at ~207 s | clean exit, 0 dropped, 0 failed |
| Tactical engine offload to executor | 239 POST timeouts / 60 s | **0 failed** / 60 s |

Tactical engine timing observed on 5-scenario benchmark (1161 ingests, 45 ticks, 150 s wall time):

```
tactical.p50_ms = 1100.6
tactical.p95_ms = 1920.3
tactical.max_ms = 2115.5
tactical.failed = 0
tactical.overlap_skipped = 0
```

A tactical tick takes up to 2.1 s under load. On the event loop, that would freeze `/ingest` for 1–2 s per tick. The executor offload removes this entirely.

---

## Running Tests

```bash
pytest tests/ -v                          # 184 unit tests
python scripts/smoke_test.py --duration 12  # end-to-end
```

CI runs both on every push to `main` (GitHub Actions, Python 3.10 / 3.11 / 3.12).

---

## Event Model

**Track ingest** (sensor agent → COP):
```json
{
  "event_type": "cop.track",
  "payload": {
    "id": "T-R012-A018",
    "lat": 41.020, "lon": 28.985,
    "intent": "attack",
    "threat_level": "HIGH",
    "classification": {"label": "drone", "confidence": 0.85},
    "supporting_sensors": ["radar-01", "rf-01"],
    "kinematics": {"range_m": 1200.0, "az_deg": 185.0, "speed_mps": 32.0}
  }
}
```

**AI update** (COP → browser, WebSocket):
```json
{
  "event_type": "cop.ai_update",
  "payload": {
    "predictions":    {"T-R012-A018": [ ...12 future points... ]},
    "recommendations": [{"type": "ESCALATE", "priority": 1, "track_ids": [...]}],
    "coord_attacks":   [{"subtype": "PINCER", "count": 8, "time_to_convergence_s": 23}],
    "roe_advisories":  [{"engagement": "WEAPONS_TIGHT", "urgency": "HIGH"}],
    "ml_predictions":  {"T-R012-A018": {"ml_level": "HIGH", "ml_probability": 0.94}}
  }
}
```

**Lineage query** (`GET /api/ai/lineage/T-R012-A018`):
```json
{
  "track_id": "T-R012-A018",
  "summary": {"count": 9, "stages": ["anomaly","coord_attack","fire_control","ingest",...], "first": "...", "last": "..."},
  "chain": [
    {"stage": "ingest",       "summary": "Track update — sensors: radar-01, rf-01", "timestamp": "..."},
    {"stage": "threat_assess","summary": "Threat → HIGH (score=92, intent=attack)",  "timestamp": "..."},
    {"stage": "ml_threat",    "summary": "RandomForest → HIGH (0.94)",               "timestamp": "..."},
    ...
    {"stage": "fire_control", "summary": "ENGAGE approved → effector launched",      "timestamp": "..."}
  ]
}
```

---

## Aerospace Ground Systems Mission Scenario

NIZAM can serve as a real-time situational awareness and decision-support layer for aerospace ground operations where multiple heterogeneous sensors must be correlated into a single operational picture.

**Application domains:**
- Launch-site and spaceport perimeter security
- Ground-station monitoring for space missions
- Autonomous facility surveillance for aerospace infrastructure
- Pre-launch and post-landing operational awareness

**Example mission flow:**
1. Multiple sensors generate track events around a launch facility
2. NIZAM ingests events via standardized interfaces (radar ASTERIX, ADS-B, AIS, REST)
3. The system maintains authoritative state: who is where, doing what, with what confidence
4. A synchronized COP streams to all connected operators in real time
5. Operators approve or reject AI-generated engagement recommendations
6. Every decision is recorded in the lineage chain for post-mission audit

---

## Scope and Limitations

This is a technical prototype for demonstration and educational purposes. It does **not** represent an active or deployed military system.

**In scope:** architecture, real-time behavior, AI decision support, multi-sensor fusion, decision lineage, operator-centric COP design, performance hardening.

**Out of scope:** classified data handling, fielded-grade security, production key management, live effector integration.

---

## Author

**Emre Altunbulak** — Mechanical Engineer

Focus areas: Command & Control Systems, Real-Time Operational Software, COP / ISR Architectures, AI Decision Support.

---

## Keywords

Common Operational Picture · C2 · ISR · Defense Software · Real-Time Systems · Event-Driven Architecture · Multi-Sensor Fusion · AI Decision Support · Decision Lineage · Decision Provenance · Aerospace Ground Systems · Anduril Lattice · Palantir Gotham · FastAPI · WebSocket · Leaflet
