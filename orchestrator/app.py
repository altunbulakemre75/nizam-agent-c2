from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="NIZAM Orchestrator", version="0.2.0")

# -----------------------
# Config
# -----------------------
AGENT_DEAD_AFTER_S = 15.0   # agent is DEAD if no heartbeat for this many seconds

# -----------------------
# In-memory state
# -----------------------
AGENTS: Dict[str, Dict[str, Any]] = {}
EVENTS_COUNT: int = 0
EVENTS: List[Dict[str, Any]] = []


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def _utc_ts() -> float:
    return datetime.now(timezone.utc).timestamp()

def _bump_event(evt: Dict[str, Any]) -> None:
    global EVENTS_COUNT, EVENTS
    EVENTS_COUNT += 1
    EVENTS.append(evt)
    if len(EVENTS) > 50:
        EVENTS = EVENTS[-50:]

def _agent_status(agent: Dict[str, Any]) -> str:
    last_ts = agent.get("last_seen_ts")
    if last_ts is None:
        return "UNKNOWN"
    if _utc_ts() - last_ts > AGENT_DEAD_AFTER_S:
        return "DEAD"
    return "ALIVE"


# -----------------------
# Startup: background health checker
# -----------------------
@app.on_event("startup")
async def start_health_checker():
    asyncio.create_task(_health_checker())

async def _health_checker():
    """Periodically update agent status fields."""
    while True:
        await asyncio.sleep(5)
        for name, agent in AGENTS.items():
            agent["status"] = _agent_status(agent)


# -----------------------
# Request models
# -----------------------
class AgentRegister(BaseModel):
    name: str           # e.g. "cop-publisher"
    url: str = ""       # optional for pipeline agents
    capabilities: List[str] = []
    metadata: Dict[str, Any] = {}

class AgentHeartbeat(BaseModel):
    name: str
    status: str = "ALIVE"
    metrics: Dict[str, Any] = {}

class TaskRequest(BaseModel):
    action: str
    payload: Dict[str, Any] = {}


# -----------------------
# Endpoints
# -----------------------
@app.get("/health")
def health():
    total = len(AGENTS)
    alive = sum(1 for a in AGENTS.values() if _agent_status(a) == "ALIVE")
    return {
        "project": "nizam",
        "status": "ok",
        "agents_total": total,
        "agents_alive": alive,
        "agents_dead": total - alive,
    }


@app.post("/agents/register")
def register_agent(body: AgentRegister):
    now = _utc_now()
    if body.name in AGENTS:
        # re-registration: update fields, keep registered_at
        AGENTS[body.name].update({
            "url": body.url,
            "capabilities": body.capabilities,
            "metadata": body.metadata,
            "last_seen": now,
            "last_seen_ts": _utc_ts(),
            "status": "ALIVE",
        })
    else:
        AGENTS[body.name] = {
            "url": body.url,
            "capabilities": body.capabilities,
            "metadata": body.metadata,
            "registered_at": now,
            "last_seen": now,
            "last_seen_ts": _utc_ts(),
            "status": "ALIVE",
        }
    _bump_event({"type": "agent_registered", "name": body.name, "ts": now})
    return {"ok": True, "registered": body.name}


@app.post("/agents/heartbeat")
def agent_heartbeat(body: AgentHeartbeat):
    now = _utc_now()
    if body.name not in AGENTS:
        # auto-register unknown agents on first heartbeat
        AGENTS[body.name] = {
            "url": "",
            "capabilities": [],
            "metadata": {},
            "registered_at": now,
            "last_seen": now,
            "last_seen_ts": _utc_ts(),
            "status": "ALIVE",
        }
    else:
        AGENTS[body.name]["last_seen"] = now
        AGENTS[body.name]["last_seen_ts"] = _utc_ts()
        AGENTS[body.name]["status"] = "ALIVE"
        if body.metrics:
            AGENTS[body.name]["metrics"] = body.metrics
    return {"ok": True}


@app.get("/agents")
def list_agents():
    result = []
    for name, data in AGENTS.items():
        result.append({
            "name": name,
            "status": _agent_status(data),
            "last_seen": data.get("last_seen"),
            "url": data.get("url", ""),
            "capabilities": data.get("capabilities", []),
            "metrics": data.get("metrics", {}),
        })
    return {"agents": result}


@app.get("/agents/health")
def agents_health():
    result = []
    for name, data in AGENTS.items():
        status = _agent_status(data)
        result.append({
            "name": name,
            "status": status,
            "last_seen": data.get("last_seen"),
            "metrics": data.get("metrics", {}),
        })
    alive = sum(1 for r in result if r["status"] == "ALIVE")
    return {
        "ok": True,
        "total": len(result),
        "alive": alive,
        "dead": len(result) - alive,
        "agents": result,
        "server_time": _utc_now(),
    }


@app.get("/agents/registry")
def agent_registry():
    return {"ok": True, "agents": AGENTS}


@app.post("/run")
def run_task(task: TaskRequest):
    source = task.payload.get("source") if isinstance(task.payload, dict) else None
    if source and source not in AGENTS:
        raise HTTPException(status_code=400, detail="Agent not registered")
    evt = {
        "type": "task",
        "action": task.action,
        "payload": task.payload,
        "source": source,
        "ts": _utc_now(),
    }
    _bump_event(evt)
    return {"ok": True, "accepted": True, "event_id": EVENTS_COUNT}


@app.get("/state")
def state():
    return {
        "ok": True,
        "events_count": EVENTS_COUNT,
        "agents": AGENTS,
        "recent_events": EVENTS[-10:],
    }
