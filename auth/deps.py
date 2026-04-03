"""
auth/deps.py — FastAPI auth dependencies.

JWT validation + role-based access control.
When AUTH_ENABLED=false (default), all dependencies are no-ops.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User, UserRole
from db.session import get_db

# ── Config ──────────────────────────────────────────────────
SECRET_KEY    = os.environ.get("JWT_SECRET", "nizam-dev-secret-CHANGE-in-production")
ALGORITHM     = "HS256"
EXPIRE_MINUTES = int(os.environ.get("JWT_EXPIRE_MINUTES", "480"))  # 8 hours
AUTH_ENABLED  = os.environ.get("AUTH_ENABLED", "false").lower() == "true"

security = HTTPBearer(auto_error=False)


# ── Token creation ───────────────────────────────────────────

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    payload = data.copy()
    expire  = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=EXPIRE_MINUTES))
    payload["exp"] = expire
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


# ── Token validation helper ──────────────────────────────────

def _decode_token(token: str) -> Optional[str]:
    """Returns username from token or None on failure."""
    try:
        payload  = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        return username if isinstance(username, str) else None
    except JWTError:
        return None


# ── FastAPI dependencies ─────────────────────────────────────

async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    token_qs:    Optional[str] = Query(None, alias="token"),   # WS query-param fallback
    db:          Optional[AsyncSession] = Depends(get_db),
) -> Optional[User]:
    """
    Returns the authenticated User, or None when AUTH_ENABLED=false.
    Supports Bearer header AND ?token= query param (for WebSocket).
    """
    if not AUTH_ENABLED:
        return None

    # Try header first, then query param
    raw_token = None
    if credentials:
        raw_token = credentials.credentials
    elif token_qs:
        raw_token = token_qs

    if not raw_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    username = _decode_token(raw_token)
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    if db is None:
        # No DB — accept any valid token (dev mode)
        return None

    result = await db.execute(select(User).where(User.username == username))
    user   = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def require_operator():
    """Requires OPERATOR or ADMIN role. No-op when AUTH_ENABLED=false."""
    async def _check(user: Optional[User] = Depends(get_current_user)) -> Optional[User]:
        if not AUTH_ENABLED:
            return user
        if user is None or user.role not in (UserRole.OPERATOR, UserRole.ADMIN):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Operator role required")
        return user
    return _check


def require_admin():
    """Requires ADMIN role. No-op when AUTH_ENABLED=false."""
    async def _check(user: Optional[User] = Depends(get_current_user)) -> Optional[User]:
        if not AUTH_ENABLED:
            return user
        if user is None or user.role != UserRole.ADMIN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
        return user
    return _check
