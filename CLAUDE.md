# NIZAM COP — Developer Notes

Anduril Lattice-inspired real-time Common Operating Picture. Python + FastAPI
backend, vanilla JS frontend, WebSocket fan-out, optional Postgres/Timescale
persistence. Single-binary dev run, Docker/K8s production.

## Run

```bash
python -m uvicorn cop.server:app --reload --port 8100     # dev
python -m pytest tests/ -q                                 # 604 tests
docker compose up                                          # full stack
```

Open http://localhost:8100 — single-page UI, WebSocket auto-connects.

## Project layout

```
cop/
  server.py               FastAPI app, lifespan, router wiring — ~500 lines
  state.py                Shared mutable state (STATE, METRICS, locks)
  ws_broadcast.py         WebSocket fan-out helper
  db_writes.py            Fire-and-forget Postgres persistence
  helpers.py              utc_now_iso, new_id
  engine/
    ai_pipeline.py        Per-tick AI orchestrator (runs every analyzer)
  routers/                25 APIRouter modules — one per domain
    ingest.py             POST /ingest + zone-breach + auto-task
    tasks.py              Approve/reject + fire-control pipeline
    ws.py                 WebSocket /ws
    reset.py              POST /api/reset
    ...                   (reads, zones, assets, waypoints, metrics, ...)
ai/
  tactical.py             Recommendation engine (one analyzer)
  predictor.py            Kalman track prediction
  ml_threat.py            RandomForest threat classifier
  fusion.py               Multi-sensor covariance-weighted fusion
  anomaly.py              Speed/heading/swarm anomaly detection
  registry.py             Analyzer plugin registry
  retrainer.py            Online retraining (operator feedback -> model)
  drift.py                PSI drift monitor
  ...                     20+ other analyzer modules
tests/                    604 tests, pytest
cop/static/               Vanilla JS frontend (app.js, ws-client.js, panels.js)
replay/                   JSONL scenario record/playback
```

## Two `tactical` modules — read this before touching either

There are two things with "tactical" in the name. They are **not** the same:

- **`ai/tactical.py`** — a single analyzer. Generates recommendations
  (intercept, reposition, escalate, withdraw, monitor, zone_warning).
  Stateless, returns a list of `TacticalRecommendation` dicts.

- **`cop/engine/ai_pipeline.py`** — the per-tick orchestrator that runs
  `ai/tactical.py` **and** every other analyzer (anomaly, coord_attack,
  ML threat, ROE, confidence, ...). Owns the thread-pool executor, rate
  limit, snapshot capture, and results broadcast.

The old name `cop/engine/tactical.py` was renamed to `ai_pipeline.py`
specifically to end the "which `tactical` am I importing?" confusion.

## Hot path (what happens on every sensor event)

```
POST /ingest
  -> cop/routers/ingest.py
      - rate limit + circuit breaker + API key
      - pydantic validation (schemas.models.EventEnvelope)
      - STATE mutation (under STATE_LOCK):
          deconfliction.find_match      -> canonical id
          fusion.engine.update          -> fused position
          track_fsm.on_update           -> lifecycle state
          STATE["tracks"][id] = payload
          breadcrumb trail append
          zone breach check             -> alerts
          ew_detector + ew_ml on_track_update
          ai_pipeline.process_track     -> Kalman, trajectory, anomaly
          ai_aar + ai_lineage bookkeeping
      - asyncio.create_task(db_write(persist_track(payload)))
      - ai_pipeline.schedule_ai_tactical()   (rate-limited fire-and-forget)
      - replay_recorder.capture_frame(make_snapshot_payload)
      - broadcast(ev)                        (fan-out to all WS clients)
```

The `schedule_ai_tactical()` call spawns a background task that snapshots
state, runs the heavy compute (swarm, coord-attack, ML, ROE) in a thread
pool executor, then broadcasts `cop.ai_update`. This was the fix for the
"/ingest timeouts under multi-drone load" incident — never block the
event loop with CPU work.

## WebSocket protocol

Client connects to `/ws?operator_id=OPS-xxx` (optional JWT `token=` when
`AUTH_ENABLED`). Server sends `cop.operator_joined` -> `cop.snapshot`
(full state) -> then incremental `cop.track`, `cop.threat`, `cop.alert`,
`cop.task`, `cop.ai_update`, `cop.effector_*`, `cop.ping` (10 s heartbeat).

On disconnect: release any `TRACK_CLAIMS`, broadcast `cop.operator_left`.

## State model

`cop/state.py` owns the module-level mutables. **Every mutation happens
under `STATE_LOCK`** (an `asyncio.Lock`) to keep concurrent /ingest and
background tasks consistent. Tests mutate state directly via `srv.STATE`
— this is intentional, not a leak, because the test suite doesn't spin
up the full server in most cases.

`AI_*` lists/dicts live alongside `STATE` but are written by analyzers
rather than /ingest. The pipeline applies results atomically at the end
of each tick.

## Testing

- `pytest tests/ -q` — 604 tests, under 15 s on a laptop.
- `tests/test_server_tactical.py` exercises the pipeline orchestrator.
- `tests/test_ingest.py` hits the ingest router directly (no TestClient).
- `tests/test_stress.py` (`@pytest.mark.slow`) — parallel ingest + p95.
- Heavy ML/fusion tests run offline models; no network calls.
- Tests that patch module-level vars must target the **canonical** module
  (`cop.routers.ingest._rate_buckets`, `cop.routers.ws.AUTH_ENABLED`)
  not `cop.server.*`. `srv.CLIENTS` etc. still work because server.py
  re-exports them for legacy tests.

## Known issues

- AAR button overlap in top-right header (CSS z-index).
- Tactical pipeline p95 latency under stress — profile first, fix after.
- `ai/retrainer.py` online retraining: works, but only 3 real feedback
  records exist; auto-threshold is 50. Drift baseline not yet locked.
- `ai/ml_threat.py` lacks a `status()` function (API endpoint exists but
  proxies directly through the module).

## Don't do

- Don't add `time.time()` or `datetime.now()` — use `shared.clock.get_clock()`
  so tests stay deterministic.
- Don't call blocking code from async handlers. Use `run_in_executor` or
  `asyncio.create_task(db_write(...))`.
- Don't import `cop.server` from routers — routers import from `cop.state`,
  `cop.db_writes`, `cop.ws_broadcast`. server.py is the top of the graph.
- Don't add another `tactical.py`. You've been warned.
