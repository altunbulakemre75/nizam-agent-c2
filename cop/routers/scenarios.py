"""cop/routers/scenarios.py  —  Scenario file CRUD."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

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
