from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


# -----------------------
# FastAPI
# -----------------------
app = FastAPI(title="NIZAM Orchestrator", version="0.1.0")


# -----------------------
# In-memory state
# -----------------------
AGENTS: Dict[str, Dict[str, Any]] = {}
EVENTS_COUNT: int = 0
EVENTS: List[Dict[str, Any]] = []  # keep last 50 events for debugging


def _bump_event(evt: Dict[str, Any]) -> None:
    global EVENTS_COUNT, EVENTS
    EVENTS_COUNT += 1
    EVENTS.append(evt)
    if len(EVENTS) > 50:
        EVENTS = EVENTS[-50:]


# -----------------------
# Request models (minimal, no external imports needed)
# -----------------------
class TaskRequest(BaseModel):
    action: str
    payload: Dict[str, Any] = {}


class AgentRegister(BaseModel):
    name: str  # e.g. "camera-1"
    url: str   # e.g. "http://127.0.0.1:8001"


# -----------------------
# Core endpoints
# -----------------------
@app.get("/health")
def health():
    return {"project": "nizam", "status": "ok"}


@app.get("/agents")
def list_agents():
    # Modules are informational; runtime registry is in /agents/registry
    return {
        "agents": [
            {"name": "camera", "module": "agents.camera_agent"},
            {"name": "machine", "module": "agents.machine_agent"},
        ]
    }


@app.post("/agents/register")
def register_agent(body: AgentRegister):
    AGENTS[body.name] = {
        "url": body.url,
        "last_seen": datetime.utcnow().isoformat() + "Z",
        "registered_at": datetime.utcnow().isoformat() + "Z",
    }
    _bump_event(
        {
            "type": "agent_registered",
            "name": body.name,
            "url": body.url,
            "ts": datetime.utcnow().isoformat() + "Z",
        }
    )
    return {"ok": True, "registered": body.name, "agent": AGENTS[body.name]}


@app.get("/agents/registry")
def agent_registry():
    return {"ok": True, "agents": AGENTS}


@app.post("/run")
def run_task(task: TaskRequest):
    """
    For now, /run is the central ingestion point for events/commands.

    We require payload.source to be registered when present (e.g. "camera-1").
    This prevents random/untrusted senders in a real system.
    """
    source = None
    if isinstance(task.payload, dict):
        source = task.payload.get("source")

    # If source specified, enforce registration
    if source and source not in AGENTS:
        raise HTTPException(status_code=400, detail="Agent not registered")

    evt = {
        "type": "task",
        "action": task.action,
        "payload": task.payload,
        "source": source,
        "ts": datetime.utcnow().isoformat() + "Z",
    }
    _bump_event(evt)

    # Minimal response for now
    return {"ok": True, "accepted": True, "event_id": EVENTS_COUNT}


@app.get("/state")
def state():
    return {
        "ok": True,
        "events_count": EVENTS_COUNT,
        "agents": AGENTS,
        "recent_events": EVENTS[-10:],  # show last 10
    }
