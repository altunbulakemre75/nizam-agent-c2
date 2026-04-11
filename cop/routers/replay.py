"""cop/routers/replay.py  —  Scenario recording playback control."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from replay import player as replay_player
from replay import recorder as replay_recorder

router = APIRouter(tags=["replay"])


@router.get("/api/replay/recordings")
async def api_replay_list():
    """List all available recordings."""
    recordings = replay_player.list_recordings()
    return JSONResponse({"recordings": recordings})


@router.post("/api/replay/load")
async def api_replay_load(req: Request):
    """Load a recording for playback."""
    body = await req.json()
    filename = body.get("filename")
    if not filename:
        return JSONResponse({"ok": False, "error": "filename required"}, status_code=400)
    try:
        player = replay_player.get_player()
        info = player.load(filename)
        return JSONResponse({"ok": True, "info": info})
    except (FileNotFoundError, ValueError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)


@router.post("/api/replay/play")
async def api_replay_play(req: Request):
    """Start or resume playback."""
    body = await req.json() if req.headers.get("content-length", "0") != "0" else {}
    speed = float(body.get("speed", 1.0))
    player = replay_player.get_player()
    player.play(speed=speed)
    return JSONResponse({"ok": True, "info": player.get_info()})


@router.post("/api/replay/pause")
async def api_replay_pause():
    """Pause playback."""
    player = replay_player.get_player()
    player.pause()
    return JSONResponse({"ok": True, "info": player.get_info()})


@router.post("/api/replay/stop")
async def api_replay_stop():
    """Stop playback and unload recording."""
    player = replay_player.get_player()
    player.stop()
    return JSONResponse({"ok": True, "info": player.get_info()})


@router.post("/api/replay/seek")
async def api_replay_seek(req: Request):
    """Seek to a specific time."""
    body = await req.json()
    elapsed_s = float(body.get("elapsed_s", 0))
    player = replay_player.get_player()
    player.seek(elapsed_s)
    return JSONResponse({"ok": True, "info": player.get_info()})


@router.post("/api/replay/speed")
async def api_replay_speed(req: Request):
    """Change playback speed."""
    body = await req.json()
    speed = float(body.get("speed", 1.0))
    player = replay_player.get_player()
    player.set_speed(speed)
    return JSONResponse({"ok": True, "info": player.get_info()})


@router.get("/api/replay/frame")
async def api_replay_frame(t: Optional[float] = Query(None)):
    """Get the current (or specified) replay frame."""
    player = replay_player.get_player()
    if player.state == "IDLE":
        return JSONResponse({"ok": False, "error": "no recording loaded"}, status_code=400)
    if t is not None:
        frame = player.get_frame_at(t)
    else:
        frame = player.get_current_frame()
    info = player.get_info()
    return JSONResponse({
        "ok": True,
        "info": info,
        "frame": frame.get("state") if frame else None,
    })


@router.get("/api/replay/status")
async def api_replay_status():
    """Get current replay status."""
    player = replay_player.get_player()
    return JSONResponse({
        "player": player.get_info(),
        "recorder": replay_recorder.get_status(),
    })
