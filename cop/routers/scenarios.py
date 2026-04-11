"""cop/routers/scenarios.py  —  Scenario file CRUD + in-process runner."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from cop.engine import scenario_runner

try:
    from auth.deps import require_operator
except Exception:
    def require_operator():
        return lambda: None

router = APIRouter(tags=["system"])

_SCENARIOS_DIR = Path(__file__).parent.parent.parent / "scenarios"


def _safe_name(name: str) -> str:
    return name.replace("/", "").replace("\\", "").replace("..", "")


@router.get("/api/scenarios")
async def api_scenarios_list():
    """List all available scenario files."""
    _SCENARIOS_DIR.mkdir(exist_ok=True)
    scenarios = []
    for f in sorted(_SCENARIOS_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            scenarios.append({
                "name":        f.stem,
                "description": data.get("description", ""),
                "duration_s":  data.get("duration_s", 300),
                "rate_hz":     data.get("rate_hz", 1.0),
                "entity_count": len(data.get("entities", [])),
            })
        except Exception:
            scenarios.append({"name": f.stem, "description": "", "duration_s": 300,
                              "rate_hz": 1.0, "entity_count": 0})
    return JSONResponse({"scenarios": scenarios})


@router.get("/api/scenarios/{name}")
async def api_scenario_get(name: str):
    """Get a specific scenario by name."""
    path = _SCENARIOS_DIR / f"{_safe_name(name)}.json"
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")))


@router.post("/api/scenarios")
async def api_scenario_save(req: Request, _=Depends(require_operator())):
    """Save (create or overwrite) a scenario file."""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    name = _safe_name(body.get("name", "").strip())
    if not name:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    _SCENARIOS_DIR.mkdir(exist_ok=True)
    path = _SCENARIOS_DIR / f"{name}.json"
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return JSONResponse({"ok": True, "name": name})


@router.delete("/api/scenarios/{name}")
async def api_scenario_delete(name: str, _=Depends(require_operator())):
    """Delete a scenario file."""
    path = _SCENARIOS_DIR / f"{_safe_name(name)}.json"
    if not path.exists():
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    path.unlink()
    return JSONResponse({"ok": True})


# ── In-process scenario runner ──────────────────────────────────────────────

@router.post("/api/scenarios/{name}/run")
async def api_scenario_run(name: str, _=Depends(require_operator())):
    """
    Start playing a scenario inside the COP server process. Generates
    synthetic cop.track + cop.threat events; the existing AI pipeline
    runs on top untouched. Use case: investor demo, single-click playback
    on Railway / single-node deployments where the agent fleet isn't up.
    """
    result = scenario_runner.start(_safe_name(name))
    code = 200 if result.get("ok") else 409
    return JSONResponse(result, status_code=code)


@router.post("/api/scenarios/stop")
async def api_scenario_stop(_=Depends(require_operator())):
    """Stop the currently-running scenario after its current tick."""
    return JSONResponse(scenario_runner.stop())


@router.get("/api/scenarios/runner/status")
async def api_scenario_runner_status():
    """Current scenario runner state (running, current tick, etc.)."""
    return JSONResponse(scenario_runner.status())
