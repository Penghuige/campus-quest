# backend/app/workers/session_source.py
"""Per-job engine/session source for Celery task bodies (hardening wave
1, P0-3; docs/quality/backend-engineering.md §12 worker rules).

WHY a shared source: a Celery task body is a SYNC function, so every
job drives its coroutine with ``asyncio.run`` — each invocation runs on
a fresh event loop that is closed when the call returns. The pinned
invariant: a process-wide pooled ``AsyncEngine`` (and its asyncpg
connections) must never be implicitly reused across those per-task
loops. Pooled connections stay bound to the loop they were first used
on; a later task borrowing one from the pool fails in ways that depend
on timing (the final-review reproduction saw intermittent
``RuntimeError: Event loop is closed`` across consecutive task
executions — an observed failure shape, not a deterministic
alternation, which is exactly what made it slip through). That
exception is in no ``autoretry_for`` list, and under
``task_acks_late=True`` Celery's default failure-ack semantics still
acknowledge the message, so with no beat/watchdog the task instance
never recovers.

The fix is the pattern rebuild_rankings / validate_submission already
validate: ONE private engine per job invocation, created and disposed
INSIDE the job's own ``asyncio.run`` scope. Every DB-touching job
composes through this module (pinned by
tests/workers/test_session_source.py);
``app.db.session.get_async_session_maker`` belongs to the FastAPI
request scope (one long-lived loop), never to a Celery body. Pool
semantics follow ``app.db.session.create_db_engine``
(``pool_pre_ping=True``) exactly like the two verified jobs' local
copies.

Imports of sqlalchemy / app.db stay INSIDE the functions (the
celery_app lazy-construction contract: importing this module never
requires a configured environment).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.config import Settings


def _resolve_settings(settings: Settings | None) -> Settings:
    from app.core.config import get_settings

    return get_settings() if settings is None else settings


def job_session_source(settings: Settings | None = None) -> Any:
    """A ``() -> async context manager`` yielding one ``AsyncSession``.

    The shared form of the per-job source rebuild_rankings and
    validate_submission carry as their local ``_default_session_source``
    (this factory covers that exact shape, so their copies can collapse
    here the next time those modules are touched): each CALL creates a
    fresh engine via the production factory ``create_db_engine``, the
    session comes from a maker over it, and the engine is disposed in a
    finally before the surrounding ``asyncio.run`` loop can close.
    Returning the CALLABLE (not a context-manager instance) keeps the
    injected-source contract those jobs' signatures already promise.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.session import create_db_engine

    resolved = _resolve_settings(settings)

    @asynccontextmanager
    async def _session() -> AsyncIterator[AsyncSession]:
        engine = create_db_engine(resolved)
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session:
                yield session
        finally:
            await engine.dispose()

    return _session


async def run_with_session[T](
    work: Callable[[AsyncSession], Awaitable[T]],
    *,
    settings: Settings | None = None,
) -> T:
    """Run ``work`` on one session over a per-call engine, then dispose.

    Call inside the job's own ``asyncio.run`` — e.g.
    ``asyncio.run(run_with_session(fetch))``: the engine's whole
    lifecycle (creation, the session, ``dispose``) then shares that one
    loop, so nothing pooled ever crosses a loop boundary.
    """
    async with job_session_source(settings)() as session:
        return await work(session)


async def run_with_session_maker[T](
    work: Callable[[async_sessionmaker[AsyncSession]], Awaitable[T]],
    *,
    settings: Settings | None = None,
) -> T:
    """Run ``work`` with a session MAKER over a per-call engine, then
    dispose.

    For collaborators that open their own sessions from a maker inside
    one ``asyncio.run`` scope (``DeliveryService`` and its
    claim-status resolver): every session the maker hands out sits on
    the same per-call engine, disposed only after ``work`` returns.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.session import create_db_engine

    engine = create_db_engine(_resolve_settings(settings))
    try:
        return await work(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()
