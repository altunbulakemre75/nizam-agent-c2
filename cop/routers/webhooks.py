"""cop/routers/webhooks.py  —  Webhook registration (operator-only)."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from cop import audit as cop_audit
from cop import webhooks as cop_webhooks

try:
    from auth.deps import require_operator
except Exception:
    def require_operator():
        return lambda: None

router = APIRouter(tags=["system"])


@router.get("/api/webhooks")
async def api_webhooks_list(_=Depends(require_operator())):
    """List registered webhook URLs."""
    return JSONResponse({"webhooks": cop_webhooks.list_webhooks()})


@router.post("/api/webhooks")
async def api_webhooks_register(req: Request, current_user=Depends(require_operator())):
    """Register a new webhook URL."""
    body = await req.json()
    url = (body.get("url") or "").strip()
    if not url.startswith("http"):
        return JSONResponse({"ok": False, "error": "url must start with http(s)"}, status_code=400)
    added = cop_webhooks.register(url)
    asyncio.create_task(cop_audit.log_action(
        username=getattr(current_user, "username", "anonymous"),
        role=getattr(current_user, "role", ""),
        action="REGISTER_WEBHOOK", resource_type="webhook", resource_id=url,
        ip=req.client.host if req.client else "",
    ))
    return JSONResponse({"ok": True, "added": added, "url": url})


@router.delete("/api/webhooks")
async def api_webhooks_remove(req: Request, current_user=Depends(require_operator())):
    """Remove a registered webhook URL."""
    body = await req.json()
    url = (body.get("url") or "").strip()
    removed = cop_webhooks.unregister(url)
    asyncio.create_task(cop_audit.log_action(
        username=getattr(current_user, "username", "anonymous"),
        role=getattr(current_user, "role", ""),
        action="REMOVE_WEBHOOK", resource_type="webhook", resource_id=url,
        ip=req.client.host if req.client else "",
    ))
    return JSONResponse({"ok": removed})
