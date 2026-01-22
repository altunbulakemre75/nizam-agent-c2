\# NIZAM – Distributed Autonomous C2 Platform (Prototype)



NIZAM is an agent-based, event-driven Command \& Control (C2) platform prototype

inspired by modern autonomous defense systems (Anduril-like architectures).



The system is designed to ingest real sensor data, generate operational events,

perform decision support, and coordinate multiple autonomous agents

in a human-in-the-loop manner.



This project is a decision-support and simulation-oriented platform.

It does NOT perform lethal actions or autonomous engagement.



---



\## Project Goal



The goal of NIZAM is to demonstrate a distributed autonomous architecture capable of:



\- Real-time sensor ingestion

\- Event-based situation awareness

\- Threat-related decision support

\- Coordinated multi-agent responses

\- Centralized command \& control (C2)



Initial focus:

Low-altitude UAV (drone) threat detection around fixed facilities.



---



\## System Architecture



Sensor Agents  

→ Events  

→ Orchestrator / C2 Core  

→ Recommendations  

→ Countermeasure Agents (simulated)



---



\## Core Components



\### Orchestrator (C2 Core)

\- Central event ingestion

\- Agent registry and health tracking

\- Rule-based decision support

\- System state aggregation



Port: 8000



\### Camera Agent (Real Sensor – Prototype)

\- Uses real camera input (laptop or USB camera)

\- Performs basic motion detection

\- Converts sensor output into operational events

\- Sends events to Orchestrator



Port: 8001



\### Machine / Countermeasure Agent (Placeholder)

\- Represents response systems (jammer, interceptor, alarm, etc.)

\- Currently simulated

\- Designed for future hardware integration



Port: 8002



---



\## Event Model (Initial)



Example event produced by a sensor agent:



```json

{

&nbsp; "type": "motion\_detected",

&nbsp; "source": "camera-1",

&nbsp; "ts": "2026-01-22T12:00:00Z",

&nbsp; "data": {

&nbsp;   "motion\_pixels": 24500

&nbsp; }

}

```



Events are lightweight, timestamped and centrally logged.



---



\## Getting Started (Local Demo)



Requirements:

\- Python 3.10+

\- Webcam (built-in or USB)



Setup:

```bash

python -m venv .venv

source .venv/bin/activate

pip install fastapi uvicorn requests opencv-python

```



Run services:



Orchestrator:

```bash

uvicorn orchestrator.app:app --reload --port 8000

```



Camera Agent:

```bash

uvicorn agents.camera\_agent:app --reload --port 8001

```



Start camera loop:

```bash

POST http://127.0.0.1:8001/start?cam\_index=0

```



Check system state:

```bash

GET http://127.0.0.1:8000/state

```



---



\## Safety and Ethics



\- No autonomous lethal decisions

\- No weapon control

\- All outputs are recommendations

\- Human-in-the-loop operation



---



\## Roadmap



Phase 1 – Sensor and Event Foundation

\- Agent-based architecture

\- Real camera input

\- Event ingestion



Phase 2 – Threat and Track Modeling

\- Object tracking

\- Threat scoring

\- Time-to-target estimation



Phase 3 – Decision Support

\- Policy engine

\- Confidence and explanation output

\- Operator approval flow



Phase 4 – Simulation and Visualization

\- Scenario simulator

\- Timeline and replay

\- Common Operating Picture (COP) UI



---



\## Intended Use Cases



\- Counter-UAS decision support

\- Fixed facility protection

\- Sensor fusion experimentation

\- Defense-oriented autonomous system research

\- C2 / C4ISR software prototyping



---



\## Disclaimer



This software is a technical prototype.

It is not intended for direct operational deployment.



