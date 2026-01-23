NIZAM – Real-Time COP (Common Operational Picture)

NIZAM is a real-time Common Operational Picture (COP) prototype designed for C2 / ISR / security-oriented systems.
It demonstrates live track ingestion, WebSocket-based state synchronization, and an operator-focused map UI.

The project is intentionally lightweight but architected in a way that mirrors military-grade COP systems.

✨ Key Capabilities

Real-time WebSocket backend

Live track ingestion and rendering

Threat-based visualization (color-coded)

Restricted / exclusion zone support

Operator-controlled UI layers

Deterministic frontend rendering

Pause / buffer–ready backend architecture

🧱 System Architecture
+-------------------+        WebSocket        +--------------------+
|                   | <--------------------> |                    |
|   Frontend (UI)   |                        |   Backend (API)    |
|   Leaflet + JS    |                        |   FastAPI          |
|                   |   HTTP (REST)          |                    |
+-------------------+ <--------------------> +--------------------+

Backend

FastAPI

WebSocket endpoint for live COP updates

In-memory operational state (tracks, zones, threats)

Event-driven architecture (cop.track, cop.snapshot, cop.zone)

Frontend

Leaflet.js map engine

Operator control panel

Layer toggles (zone on/off)

Threat legend and filtering

Real-time marker updates via WebSocket

📡 Event Model (Core)
Track Event
{
  "event_type": "cop.track",
  "payload": {
    "id": "T1",
    "lat": 41.015,
    "lon": 28.979,
    "threat_score": 80
  }
}

Snapshot Event (WS)
{
  "event_type": "cop.snapshot",
  "tracks": {
    "T1": {
      "lat": 41.015,
      "lon": 28.979
    }
  },
  "paused": false
}

🚀 Running the Project
1️⃣ Backend
cd nizam-backend
.\.venv\Scripts\Activate.ps1
python -m uvicorn main:app --host 127.0.0.1 --port 8000


Health check:

http://127.0.0.1:8000/api/state


WebSocket:

ws://127.0.0.1:8000/ws

2️⃣ Frontend
cd nizam-frontend
python -m http.server 5173


Open in browser:

http://127.0.0.1:5173

🧪 Quick Test (Track Injection)
$t='{"event_type":"cop.track","payload":{"id":"T1","lat":41.015,"lon":28.979,"threat_score":80}}'
Invoke-WebRequest -Uri http://127.0.0.1:8000/api/ingest `
  -Method POST `
  -ContentType "application/json" `
  -Body $t


Expected result:

Track appears on the map

Marker color reflects threat level

Track count increases in UI

🎯 Project Scope & Intent

This project is not a toy demo.
It is a foundation-level COP system designed to demonstrate:

Real-time operational awareness

Event-driven state propagation

Operator-centric UI concepts

Expandability toward:

Sensor fusion

Track correlation

Threat scoring engines

Replay / forensic analysis

🔜 Planned Extensions

Track detail side panel

Pause / resume with buffered playback

Multi-sensor fusion (radar, RF, EO)

Persistent storage (Redis / PostgreSQL)

Role-based operator views

🛡️ Disclaimer

This project is a technical prototype for educational and demonstrative purposes.
It does not represent a deployed military or security system.

👤 Author

Emre Altunbulak
Mechanical Engineer | Real-Time Systems | C2 / COP Architectures

📌 Keywords

COP · C2 · ISR · FastAPI · WebSocket · Leaflet · Real-Time Systems · Defense Software