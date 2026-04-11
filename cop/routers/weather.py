"""cop/routers/weather.py  —  Weather observation endpoints."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from cop import weather as cop_weather
from cop.helpers import utc_now_iso

router = APIRouter(tags=["weather"])


@router.get("/api/weather")
async def api_weather(refresh: bool = False):
    """Current weather observations for all stations in the AO."""
    obs  = cop_weather.get_observations(force_refresh=refresh)
    warn = cop_weather.tactical_warnings(obs)
    return JSONResponse({
        "observations": obs,
        "warnings":     warn,
        "count":        len(obs),
        "server_time":  utc_now_iso(),
    })


@router.get("/api/weather/{station_id}")
async def api_weather_station(station_id: str):
    """Single-station weather observation."""
    obs = cop_weather.get_station(station_id.upper())
    if not obs:
        return JSONResponse({"ok": False, "error": "station not found"}, status_code=404)
    return JSONResponse({**obs, "server_time": utc_now_iso()})
