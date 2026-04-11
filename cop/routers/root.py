"""cop/routers/root.py  —  HTML page routes (index, login)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()

_templates = Jinja2Templates(directory="cop/templates")


@router.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return _templates.TemplateResponse(request=request, name="index.html")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return _templates.TemplateResponse(request=request, name="login.html")
