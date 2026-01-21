from enum import Enum
from pydantic import BaseModel
from typing import Dict, Any


class AgentName(str, Enum):
    camera = "camera"
    machine = "machine"


class TaskRequest(BaseModel):
    action: str
    payload: Dict[str, Any] = {}


class AgentResult(BaseModel):
    ok: bool
    agent: AgentName
    action: str
    data: Dict[str, Any]
    error: str = ""


class OrchestratorResponse(BaseModel):
    ok: bool
    decision: AgentName
    result: AgentResult
