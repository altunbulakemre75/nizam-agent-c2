"""cop/routers/bda.py  —  Battle Damage Assessment read endpoint."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ai import bda as ai_bda
from cop.helpers import utc_now_iso

router = APIRouter(tags=["bda"])


@router.get("/api/bda")
async def api_bda():
    """Battle Damage Assessment records — finalized outcomes and pending miss checks."""
    return JSONResponse({
        "records":     ai_bda.get_all(),
        "pending":     ai_bda.get_pending(),
        "summary":     ai_bda.summary(),
        "server_time": utc_now_iso(),
    })
