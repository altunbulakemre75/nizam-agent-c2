"""
auth/router.py — Auth endpoints: login, register, me, users list.

POST /auth/login     → { access_token, token_type, role, username }
POST /auth/register  → create user (first user = admin, rest = operator/admin only)
GET  /auth/me        → current user info
GET  /auth/users     → list all users (admin only)
DELETE /auth/users/{username} → delete user (admin only)
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from passlib.context import CryptContext
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.deps import (
    AUTH_ENABLED,
    create_access_token,
    get_current_user,
    require_admin,
)
from db.models import User, UserRole
from db.session import get_db

router = APIRouter(prefix="/auth", tags=["auth"])
pwd   = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── Request schemas ──────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    role: UserRole = UserRole.OPERATOR


# ── Endpoints ────────────────────────────────────────────────

@router.get("/status")
async def auth_status():
    """Frontend can poll this to know if auth is required."""
    return {"auth_enabled": AUTH_ENABLED}


@router.post("/login")
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    result = await db.execute(select(User).where(User.username == body.username))
    user   = result.scalar_one_or_none()

    if not user or not pwd.verify(body.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    user.last_login = datetime.now(timezone.utc)
    await db.commit()

    token = create_access_token({"sub": user.username, "role": user.role.value})
    return {
        "access_token": token,
        "token_type":   "bearer",
        "username":     user.username,
        "role":         user.role.value,
    }


@router.post("/register")
async def register(
    body:         RegisterRequest,
    db:           AsyncSession = Depends(get_db),
    current_user: User | None  = Depends(get_current_user),
):
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    # Count existing users
    result   = await db.execute(select(User))
    existing = result.scalars().all()

    is_first = len(existing) == 0

    if not is_first:
        # Only admins can create new users
        if AUTH_ENABLED and (current_user is None or current_user.role != UserRole.ADMIN):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")

    # Duplicate check
    dup = await db.execute(select(User).where(User.username == body.username))
    if dup.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")

    # First user is always ADMIN regardless of requested role
    role   = UserRole.ADMIN if is_first else body.role
    hashed = pwd.hash(body.password)
    user   = User(username=body.username, hashed_password=hashed, role=role)
    db.add(user)
    await db.commit()

    return {"ok": True, "username": user.username, "role": role.value}


@router.get("/me")
async def me(current_user: User | None = Depends(get_current_user)):
    if current_user is None:
        return {"auth_enabled": AUTH_ENABLED, "username": "anonymous", "role": "ADMIN"}
    return {
        "auth_enabled": AUTH_ENABLED,
        "username":     current_user.username,
        "role":         current_user.role.value,
    }


@router.get("/users")
async def list_users(
    db:           AsyncSession = Depends(get_db),
    _:            User | None  = Depends(require_admin()),
):
    if db is None:
        return {"users": []}
    result = await db.execute(select(User))
    users  = result.scalars().all()
    return {
        "users": [
            {"username": u.username, "role": u.role.value, "created_at": u.created_at}
            for u in users
        ]
    }


@router.delete("/users/{username}")
async def delete_user(
    username:     str,
    db:           AsyncSession = Depends(get_db),
    current_user: User | None  = Depends(require_admin()),
):
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    result = await db.execute(select(User).where(User.username == username))
    user   = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if current_user and user.username == current_user.username:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    await db.delete(user)
    await db.commit()
    return {"ok": True, "deleted": username}
