from fastapi import FastAPI
from shared.schemas import TaskRequest, AgentResult, AgentName

app = FastAPI(title="Machine Agent")


@app.post("/task")
def task(task: TaskRequest):
    return AgentResult(
        ok=True,
        agent=AgentName.machine,
        action=task.action,
        data={"message": "machine task executed"},
        error="",
    ).model_dump()
