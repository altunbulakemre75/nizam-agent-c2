NIZAM
Real-Time Common Operational Picture (COP) System

NIZAM is a real-time Common Operational Picture (COP) system prototype developed to demonstrate core concepts used in Command & Control (C2), ISR, and security-oriented operational systems.

The system focuses on live situational awareness, event-driven data flow, and operator-centric visualization, aligned with principles commonly applied in defense and security software architectures.

1. Operational Purpose

The primary objective of NIZAMIZAM is to provide a single, consistent operational picture by:

Collecting real-time track data

Maintaining a unified operational state

Broadcasting updates deterministically to all connected operators

Visualizing threats and restricted areas on a geospatial interface

The system architecture reflects Phase-1 Operational COP UI capabilities.

2. Core Capabilities

Real-time WebSocket-based C2 data distribution

Live track ingestion and state synchronization

Threat-level–based visualization (Low / Medium / High)

Restricted / exclusion zone definition and rendering

Operator-controlled UI layers (no backend coupling)

Deterministic and predictable frontend rendering

Pause- and buffer-ready backend design for future replay support

3. System Architecture Overview
+---------------------------------------------------+
|                   Operator UI                     |
|             (Leaflet-based COP View)              |
+-------------------------▲-------------------------+
                          │ WebSocket (Live State)
                          │
+-------------------------▼-------------------------+
|                Backend / C2 Core                  |
|              FastAPI + WebSocket                  |
|                                                   |
|  - Track State Management                          |
|  - Threat Context                                  |
|  - Zone Definitions                                |
|  - Event Broadcasting                              |
+-------------------------▲-------------------------+
                          │ REST (Control / Ingest)
                          │
+-------------------------▼-------------------------+
|             External Data Sources                  |
|     (Sensors, Simulators, External Systems)        |
+---------------------------------------------------+

4. Backend Design
Technology Stack

FastAPI

Python

WebSocket-based event dissemination

Stateless REST endpoints for ingestion and control

Responsibilities

Maintain authoritative operational state

Accept external track events

Broadcast COP updates to all connected clients

Provide snapshot synchronization on client connect

5. Frontend Design
Technology Stack

Leaflet.js

Vanilla JavaScript (deterministic rendering)

No framework dependency

Operator Interface Features

Real-time track visualization

Threat-based color coding

Restricted zone layer toggle

Minimum threat-level filtering

Operational legend for threat interpretation

The UI design prioritizes clarity, low cognitive load, and operational usability.

6. Event Model
Track Event (Ingest)
{
  "event_type": "cop.track",
  "payload": {
    "id": "T1",
    "lat": 41.015,
    "lon": 28.979,
    "threat_score": 80
  }
}

Snapshot Event (WebSocket)
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

7. Execution Instructions
Backend
cd nizam-backend
.\.venv\Scripts\Activate.ps1
python -m uvicorn main:app --host 127.0.0.1 --port 8000


Health endpoint:

http://127.0.0.1:8000/api/state


WebSocket endpoint:

ws://127.0.0.1:8000/ws

Frontend
cd nizam-frontend
python -m http.server 5173


Access:

http://127.0.0.1:5173

8. Operational Test (Track Injection)
$t='{"event_type":"cop.track","payload":{"id":"T1","lat":41.015,"lon":28.979,"threat_score":80}}'
Invoke-WebRequest -Uri http://127.0.0.1:8000/api/ingest `
  -Method POST `
  -ContentType "application/json" `
  -Body $t


Expected outcome:

Track appears immediately on COP UI

Marker color reflects threat level

Track count updates in real time

9. Scope and Limitations

This project represents a foundational COP capability, not a complete operational system.

Excluded by design:

Authentication / authorization

Persistent storage

Encrypted communications

Classified data handling

These elements are intentionally omitted to keep the prototype architecture-focused.

10. Planned Extensions

Track detail and analysis panels

Pause / resume with buffered playback

Multi-sensor fusion (EO, radar, RF)

Threat scoring engines

Persistent state and replay

Role-based operator views

11. Disclaimer

This software is a technical prototype developed for demonstration and educational purposes only.
It does not represent an active or deployed military system.

12. Author

Emre Altunbulak
Mechanical Engineer
Focus Areas:

Command & Control Systems

Real-Time Operational Software

COP / ISR Architectures

13. Keywords

Common Operational Picture · C2 · ISR · Defense Software ·
Real-Time Systems · WebSocket · Operational UI