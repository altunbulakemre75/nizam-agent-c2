NIZAM

Real-Time, Event-Driven Common Operational Picture (COP) System

Overview

NIZAM is a real-time Common Operational Picture (COP) system prototype designed to demonstrate foundational architectural concepts used in modern Command & Control (C2), ISR, and aerospace ground systems.

The system emphasizes event-driven data flow, deterministic state distribution, and operator-centric situational awareness, aligned with principles commonly applied in defense, security, and space ground segment software architectures.

1. Operational Purpose

The primary objective of NIZAM is to provide a single, consistent operational picture by:

Collecting real-time track and event data

Maintaining an authoritative operational state

Broadcasting deterministic updates to all connected operators

Visualizing threats and restricted areas on a geospatial COP interface

The current implementation reflects Phase-1 Operational COP UI capabilities, focusing on correctness, clarity, and real-time behavior.

2. Core Capabilities

Real-time WebSocket-based C2 state distribution

Live track ingestion and synchronized operational state

Threat-level–based visualization (Low / Medium / High)

Restricted and exclusion zone definition and rendering

Operator-controlled UI layers with no backend coupling

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
The architecture is sensor-agnostic and extensible, enabling integration of heterogeneous data sources without modifying the core COP logic.

4. Backend Design
Technology Stack

Python

FastAPI

WebSocket-based event dissemination

Stateless REST endpoints for ingestion and control

Responsibilities

Maintain the authoritative operational state

Accept external track and control events

Broadcast COP updates to all connected clients

Provide snapshot synchronization on client connection

5. Frontend Design
Technology Stack

Leaflet.js

Vanilla JavaScript (deterministic rendering)

No framework dependency

Operator Interface Features

Real-time track visualization

Threat-based color coding

Restricted zone layer toggling

Minimum threat-level filtering

Operational legend for threat interpretation

The UI design prioritizes clarity, low cognitive load, and operational usability.

6. Event Model
Track Event (Ingest)
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

Track state updates in real time

9. Scope and Limitations

This project represents a foundational COP architecture prototype, not a complete operational system.

Intentionally excluded:

Authentication and authorization

Persistent storage

Encrypted communications

Classified data handling

These elements are omitted by design to keep the focus on architecture, real-time behavior, and system clarity.

10. Planned Extensions

Track detail and analytical panels

Pause / resume with buffered playback

Multi-sensor fusion (EO, radar, RF)

Threat scoring engines

Persistent state and replay

Role-based operator views

## 🚀 Mission Scenario – Aerospace Ground Systems

### Scenario Overview
NIZAM can be adapted as a real-time situational awareness and decision-support layer for aerospace ground systems, particularly in environments where multiple heterogeneous sensors must be correlated into a single operational picture.

Potential application domains include:
- Launch site and spaceport perimeter security
- Ground station monitoring for space missions
- Autonomous facility surveillance for aerospace infrastructure
- Pre-launch and post-landing operational awareness

---

### Operational Context
Modern aerospace ground operations rely on a combination of sensors such as:
- Electro-optical cameras
- Radar-based tracking systems
- RF monitoring equipment
- Simulation and telemetry feeds

These data sources often operate independently, creating fragmented awareness and delayed operational response.

NIZAM addresses this challenge by acting as a sensor-agnostic COP layer, aggregating real-time events into a unified, deterministic operational state shared across all operators.

---

### Example Mission Flow
1. Multiple ground sensors generate track events around a launch facility
2. Events are ingested by the NIZAM backend through standardized interfaces
3. The system maintains an authoritative operational state and evaluates threat context
4. A synchronized COP is broadcast in real time to all connected operators
5. Operators visualize tracks, threat levels, and restricted zones on a shared geospatial interface

This workflow enables low-latency situational awareness, consistent decision-making, and scalable operator coordination.

---

### Relevance to Aerospace Ground Systems
The architecture and design principles demonstrated by NIZAM align with key requirements of aerospace ground systems:

- Real-time and deterministic state distribution
- Sensor-agnostic and extensible architecture
- Operator-centric situational awareness
- Support for simulation-driven testing and replay
- Clear separation between data ingestion, state management, and visualization

NIZAM is not a mission-specific system but a foundational COP architecture prototype that can be extended for aerospace, planetary surface operations, and spaceport ground support systems.

---

### Research and Extension Potential
Future extensions relevant to aerospace missions include:
- Multi-sensor fusion algorithms
- Time-synchronized event replay for mission analysis
- Autonomous alerting and anomaly detection
- Integration with simulation and digital twin environments

11. Disclaimer

This software is a technical prototype developed for demonstration and educational purposes only.
It does not represent an active or deployed military system.

12. Author

Emre Altunbulak
Mechanical Engineer

Focus Areas

Command & Control Systems

Real-Time Operational Software

COP / ISR Architectures

13. Keywords

Common Operational Picture · C2 · ISR · Defense Software ·
Real-Time Systems · Event-Driven Architecture · Aerospace Ground Systems