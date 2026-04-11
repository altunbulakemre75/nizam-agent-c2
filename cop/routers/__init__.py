"""cop/routers — extracted FastAPI routers, grouped by domain.

Each module defines a single `router = APIRouter(...)` that cop/server.py
includes via `app.include_router(router)`. Routers should depend only on
cop.state, cop.helpers, and the `ai/`/`shared/` modules — never on
cop.server itself, otherwise the circular import this split was meant to
eliminate comes right back.
"""
