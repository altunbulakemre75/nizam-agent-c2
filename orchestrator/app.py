from fastapi import FastAPI, HTTPException
import requests
from typing import Dict

from shared.schemas import (
    TaskRequest,
    OrchestratorResponse,
    AgentResult,
    AgentName,
)

app = FastAPI(title="NIZAM Orchestrator")

AGENTS: Dict[str, str] = {}  # name -> url


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/agents/register")
def register_agent(name: str, url: str):
    AGENTS[name] = url
    return {"ok": True, "agents": AGENTS}


@app.get("/agents")
def list_agents():
    return AGENTS


@app.post("/run", response_model=OrchestratorResponse)
def run(task: TaskRequest):
    # Basit karar: action içinde "camera" varsa camera, yoksa machine
    decision = (
        AgentName.camera if "camera" in task.action.lower() else AgentName.machine
    )

    agent_name = "camera-1" if decision == AgentName.camera else "machine-1"

    if agent_name not in AGENTS:
        raise HTTPException(status_code=400, detail="Agent not registered")

    try:
        r = requests.post(
            f"{AGENTS[agent_name]}/task",
            json=task.model_dump(),
            timeout=5,
        )
        r.raise_for_status()
        result = AgentResult(**r.json())
        return OrchestratorResponse(ok=True, decision=decision, result=result)

    except Exception as e:
        return OrchestratorResponse(
            ok=False,
            decision=decision,
            result=AgentResult(
                ok=False,
                agent=decision,
                action=task.action,
                data={},
                error=str(e),
            ),
        )
