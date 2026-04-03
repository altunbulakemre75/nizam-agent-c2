"""
db/session.py — SQLAlchemy async engine + session factory
Works with PostgreSQL / TimescaleDB via asyncpg.
Falls back gracefully when DATABASE_URL is not set.
"""
from __future__ import annotations

import os
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

DATABASE_URL: str | None = os.environ.get("DATABASE_URL")

# Engine is None when no DB configured (in-memory-only mode)
engine = (
    create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
    if DATABASE_URL
    else None
)

AsyncSessionLocal: async_sessionmaker | None = (
    async_sessionmaker(engine, expire_on_commit=False)
    if engine
    else None
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession | None:
    """FastAPI dependency. Yields None when DB is not configured."""
    if AsyncSessionLocal is None:
        yield None
        return
    async with AsyncSessionLocal() as session:
        yield session
