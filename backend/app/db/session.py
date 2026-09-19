# backend/app/db/session.py
"""Async engine and session management (backend-engineering §5, §8).

The engine and session factory are created lazily from typed settings so
importing this module never requires a configured environment; request
handlers receive a session through the `get_db_session` dependency.

Environment handling: `get_settings()` reads DATABASE_URL (plus the other
required deployment variables) from the process environment, so the same
environment drives the application and Alembic (see alembic/env.py).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings


def create_db_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine for `settings.database_url`.

    `pool_pre_ping=True` discards pooled connections that were dropped by a
    database restart instead of failing the request.
    """
    return create_async_engine(settings.database_url, pool_pre_ping=True)


@lru_cache
def get_async_engine() -> AsyncEngine:
    """Process-wide engine, cached per settings snapshot."""
    return create_db_engine(get_settings())


@lru_cache
def get_async_session_maker() -> async_sessionmaker[AsyncSession]:
    """Session factory bound to the cached engine.

    `expire_on_commit=False` keeps committed instances readable while response
    data is assembled without a refresh round trip; transport DTOs are still
    built explicitly and never serialized straight from ORM objects.
    """
    return async_sessionmaker(get_async_engine(), expire_on_commit=False)


def __getattr__(name: str) -> AsyncEngine | async_sessionmaker[AsyncSession]:
    """Expose `async_engine` / `async_session_maker` lazily (PEP 562)."""
    if name == "async_engine":
        return get_async_engine()
    if name == "async_session_maker":
        return get_async_session_maker()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding one session per request.

    Commit on success, rollback on error, close in every case. Transaction
    ownership stays with services and use cases (§5): they may commit inside
    the request, and the final commit of an already-completed or empty
    transaction is a no-op.
    """
    session = get_async_session_maker()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
