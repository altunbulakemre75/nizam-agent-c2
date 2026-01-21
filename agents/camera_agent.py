from fastapi import FastAPI
from shared.schemas import TaskRequest, AgentResult, AgentName

app = FastAPI(title="Camera Agent")


@app.post("/task")
def task(task: TaskRequest):
    return AgentResult(
        ok=True,
        agent=AgentName.camera,
        action=task.action,
        data={"message": "camera task executed"},
        error="",
    ).model_dump()
