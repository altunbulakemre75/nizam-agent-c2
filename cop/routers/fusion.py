"""cop/routers/fusion.py  —  Fused-tracks read endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ai import fusion as ai_fusion
from cop.helpers import utc_now_iso

try:
    from auth.deps import require_viewer
except Exception:
    def require_viewer():
        return lambda: None

router = APIRouter(tags=["fusion"])


@router.get("/api/fusion/tracks")
async def api_fusion_tracks(_=Depends(require_viewer())):
    """Return all currently active fused tracks from the fusion engine."""
    return JSONResponse({
        "fused_tracks": [t.to_dict() for t in ai_fusion.engine.all_tracks()],
        "stats":        ai_fusion.engine.stats(),
        "server_time":  utc_now_iso(),
    })
