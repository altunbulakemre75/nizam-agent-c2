# NIZAM

**Real-Time, Event-Driven Common Operational Picture (COP) System**

NIZAM is a real-time COP / C2 prototype inspired by systems like
Anduril Lattice. It demonstrates foundational architectural concepts
used in modern command & control, ISR, and aerospace ground-segment
software: event-driven data flow, deterministic state distribution,
multi-sensor fusion, AI-assisted decision support, and operator-centric
situational awareness.

The project is a self-contained reference implementation — no external
services are required to run it end-to-end.

---

## 1. What NIZAM Does

- **Ingests** synthetic sensor events (radar, RF, electro-optical)
  through an asynchronous agent pipeline.
- **Fuses** multi-sensor observations into tracks with intent
  classification and ML threat scoring.
- **Maintains** the authoritative operational state: tracks, threats,
  zones, friendly/hostile assets, tasks, waypoints.
- **Runs** an AI decision-support layer: Kalman trajectory prediction,
  swarm/coordinated attack detection, tactical recommendations,
  rules-of-engagement advisories, after-action reports.
- **Broadcasts** everything in real time over WebSocket to a Leaflet
  browser UI.

---

## 2. Capabilities (all implemented)

### Core COP

- Real-time WebSocket state distribution (tracks, threats, zones,
  assets, tasks, waypoints)
- Multi-sensor fusion: radar + RF bearing + EO/camera
- Intent classification: attack / reconnaissance / loitering
- ML threat scoring (RandomForest) with rule-based fallback
- Threat-level visualization (HIGH / MEDIUM / LOW)
- Zone system: restricted / kill / friendly polygons with ray-casting
  breach detection
- Asset management (friendly / hostile / unknown) and mission waypoints
- Autonomous task proposal with operator approve / reject workflow
- Pause / resume, reset, passthrough, replay from JSONL recordings

### AI Decision Support (Phase 5)

- Kalman-filter track prediction (60 s ahead, 12 predicted points)
- Anomaly detection: speed spikes, heading reversals, intent shifts
- Swarm detection: proximity + correlated heading clustering
- Coordinated attack detection (pincer, convergence)
- Predictive zone breach + uncertainty cones
- Tactical recommendation engine (intercept, zone warning, escalate,
  withdraw, monitor, reposition)
- Rules-of-Engagement (ROE) advisory engine
- After-Action Report (AAR) generator
- LLM operator advisor (Claude / OpenAI API, rule-based fallback)

### Platform

- PostgreSQL / TimescaleDB persistence (optional)
- JWT auth with operator roles (optional)
- Docker + Docker Compose
- Kubernetes manifests (namespace, StatefulSet, Deployment + HPA,
  Ingress)
- GitHub Actions CI: 170 pytest tests + end-to-end smoke test
- Runtime metrics endpoint for performance diagnostics

---

## 3. System Architecture

```
                 ┌─ radar_sim ─┐
  world_agent ──┼─  rf_sim   ──┼─ fuser ─ cop_publisher ─► COP /ingest
                 └─  eo_sim   ─┘                            │
                                                            ▼
                              orchestrator ◄──┐     ┌───────────────┐
                                heartbeats    └────►│  COP Server   │
                                                    │  (FastAPI)    │
                                                    │               │
                                                    │  STATE_LOCK   │
                                                    │  tactical bg  │
                                                    │  task (exec)  │
                                                    └───────┬───────┘
                                                            │ WebSocket
                                                            ▼
                                                    ┌───────────────┐
                                                    │  Browser UI   │
                                                    │  (Leaflet)    │
                                                    └───────────────┘
```

Key design properties:

- **Pipeline back-pressure is impossible.** `cop_publisher` uses a
  bounded in-memory queue with drop-oldest eviction and a worker
  thread pool. The stdin reader never blocks on HTTP.
- **The event loop never stalls on AI compute.** The tactical engine
  (swarm / coord attack / ML / ROE) runs in a `run_in_executor` thread
  pool, driven by a fire-and-forget background task that `/ingest`
  merely schedules. `/ingest` returns in milliseconds even during
  heavy AI passes.
- **Shallow state snapshots** are handed to the executor so concurrent
  `/ingest` calls never see torn dicts during iteration.

---

## 4. Repository Layout

```
agents/           sensor + fusion + cop_publisher
  cop_publisher.py        pipeline -> COP REST ingest (thread pool)
  fuser/                  multi-sensor fusion + intent + ML scoring
  radar_sim/ eo_sim/ rf_sim/ world/
ai/               Phase 5 AI decision support
  aar.py                  after-action report
  anomaly.py              anomaly + swarm detection
  coordinated_attack.py   pincer / convergence
  llm_advisor.py          Claude / OpenAI operator advisor
  ml_threat.py            RandomForest threat classifier
  predictor.py            Kalman track prediction
  roe.py                  rules of engagement
  tactical.py             recommendation engine
  timeline.py / zone_breach.py
auth/             JWT + role-based deps (optional)
cop/              FastAPI COP server (state, ingest, WS, AI hooks, API)
db/               SQLAlchemy models, Postgres / TimescaleDB session
k8s/              Kubernetes manifests
orchestrator/     agent registry + heartbeat
replay/           JSONL recorder + time-slider player
scenarios/        single_drone / swarm / coordinated / multi_axis / decoy
schemas/          event schemas
scripts/          compare_scenarios.py, smoke_test.py
shared/           heartbeat client
tests/            170 pytest tests
run_pipeline.py   pipeline launcher (fan-out into cop_publisher)
start.py          all-in-one: orchestrator + COP + pipeline
```

---

## 5. Quick Start

Single-command demo (orchestrator + COP server + pipeline):

```bash
pip install -r requirements.txt
python start.py --scenario scenarios/multi_axis_attack.json
```

Then open **http://127.0.0.1:8100** — the Leaflet COP UI.

Endpoints:

| URL | Purpose |
|---|---|
| `http://127.0.0.1:8100` | COP UI |
| `http://127.0.0.1:8100/api/metrics` | Runtime metrics (ingest, tactical timings, WS) |
| `http://127.0.0.1:8100/api/ai/status` | AI subsystem status |
| `http://127.0.0.1:8100/api/ai/aar` | After-action report |
| `http://127.0.0.1:8200` | Orchestrator (agent health) |

Multi-scenario benchmark runner:

```bash
python scripts/compare_scenarios.py --duration 30
```

Runs all five scenarios sequentially against a running COP server,
prints a comparison table, and saves a full AAR bundle to `reports/`.

End-to-end smoke test (boots server, runs pipeline, asserts metrics):

```bash
python scripts/smoke_test.py --duration 12
```

---

## 6. Performance

The two biggest hot-path bottlenecks were identified and fixed
through load testing against `multi_axis_attack` (5 simultaneous
drones / helicopters from 4 cardinal directions):

| Fix | Before | After |
|---|---|---|
| `cop_publisher` thread pool + drop-oldest queue | pipeline deadlocked at ~207 s | clean exit, 0 dropped, 0 failed |
| Server tactical engine offload to executor | 239 POST timeouts / 60 s | **0 failed** / 60 s |

Tactical engine timings observed on a 5-scenario benchmark
(1161 ingests, 45 tactical ticks, 150 s wall time):

```
tactical.p50_ms = 1100.6
tactical.p95_ms = 1920.3
tactical.max_ms = 2115.5
tactical.failed = 0
tactical.overlap_skipped = 0
```

A tactical tick takes up to 2.1 seconds under load — if that work
ran on the event loop, every tick would freeze `/ingest` for 1–2 s,
which is exactly the failure mode the executor offload removes.

---

## 7. Running Tests

```bash
pytest tests/ -v
python scripts/smoke_test.py --duration 12
```

170 unit tests + the end-to-end smoke test. Both are run in
GitHub Actions on every push to `main`.

---

## 8. Event Model (excerpt)

Track ingest (agent → COP):

```json
{
  "event_type": "cop.track",
  "payload": {
    "id": "T-001",
    "lat": 41.020,
    "lon": 28.985,
    "intent": "attack",
    "threat_level": "HIGH",
    "classification": {"label": "drone", "confidence": 0.85},
    "kinematics": {"range_m": 1200.0, "az_deg": 45.0, "speed_mps": 32.0}
  }
}
```

AI update (COP → browser, WebSocket):

```json
{
  "event_type": "cop.ai_update",
  "payload": {
    "predictions":    {"T-001": [ ... 12 future points ... ]},
    "recommendations": [ { "type": "intercept", "target": "T-001", ... } ],
    "coord_attacks":   [ { "pattern": "pincer", "tracks": [...] } ],
    "roe_advisories":  [ ... ],
    "ml_predictions":  { "T-001": {"score": 0.94, "class": "hostile"} }
  }
}
```

---

## 9. Mission Scenario — Aerospace Ground Systems

NIZAM can be adapted as a real-time situational awareness and
decision-support layer for aerospace ground systems, particularly
in environments where multiple heterogeneous sensors must be
correlated into a single operational picture.

Potential application domains include:
- Launch-site and spaceport perimeter security
- Ground-station monitoring for space missions
- Autonomous facility surveillance for aerospace infrastructure
- Pre-launch and post-landing operational awareness

### Operational Context

Modern aerospace ground operations rely on a combination of sensors
such as electro-optical cameras, radar-based tracking, RF monitoring,
and simulation/telemetry feeds. These sources often operate
independently, creating fragmented awareness and delayed operational
response.

NIZAM addresses this by acting as a sensor-agnostic COP layer,
aggregating real-time events into a unified, deterministic operational
state shared across all operators.

### Example Mission Flow

1. Multiple ground sensors generate track events around a launch facility.
2. Events are ingested by the NIZAM backend through standardized interfaces.
3. The system maintains an authoritative operational state and evaluates
   threat context.
4. A synchronized COP is broadcast in real time to all connected operators.
5. Operators visualize tracks, threat levels, predicted trajectories,
   coordinated-attack warnings, and restricted zones on a shared
   geospatial interface.

### Relevance to Aerospace Ground Systems

- Real-time and deterministic state distribution
- Sensor-agnostic and extensible architecture
- Operator-centric situational awareness
- Simulation-driven testing and replay
- Clear separation between data ingestion, state management,
  AI decision support, and visualization

NIZAM is not a mission-specific system but a foundational COP
architecture prototype that can be extended for aerospace,
planetary surface operations, and spaceport ground support systems.

---

## 10. Scope and Limitations

This project is a technical prototype developed for demonstration
and educational purposes. It does **not** represent an active or
deployed military system.

Intentionally in scope:
- Architecture, real-time behaviour, AI decision support
- Multi-sensor fusion and operator-centric COP design
- Performance hardening under load

Intentionally out of scope:
- Real sensor integration
- Classified data handling
- Fielded-grade security posture

---

## 11. Author

**Emre Altunbulak** — Mechanical Engineer

Focus areas: Command & Control Systems, Real-Time Operational
Software, COP / ISR Architectures.

## 12. Keywords

Common Operational Picture · C2 · ISR · Defense Software ·
Real-Time Systems · Event-Driven Architecture · Multi-Sensor Fusion ·
AI Decision Support · Aerospace Ground Systems
