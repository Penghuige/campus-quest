# backend/app/core/readiness.py
"""Readiness components and registry for `/health/ready` (spec §34).

Liveness and readiness are deliberately decoupled: `/health/live` answers
200 as long as the process runs and never consults a dependency, while
`/health/ready` reports per-component availability so a orchestrator can
stop routing traffic to an instance that cannot serve requests.

Component scope (spec §34): PostgreSQL and Redis are required for ready.
Object storage (S3) is intentionally NOT part of readiness — §34 lets a
deployment exclude object storage (and SMS/email providers) so a transient
third-party blip cannot remove otherwise-working API instances. Uploads
would degrade, but reads, auth, and task flow keep working without S3.

The registry is injectable: handlers receive it through FastAPI dependency
injection on `get_readiness_registry`, so tests substitute a registry of
fake components without touching real services.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, Protocol

import redis.asyncio as aioredis
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import get_async_engine

ComponentStatus = Literal["ok", "down"]


@dataclass(frozen=True)
class ReadinessReport:
    """Machine-readable readiness result with a stable body shape.

    `components` maps each component name to "ok" or "down" so callers can
    act on specific failures, matching the endpoint contract
    `{"status": "available"|"unavailable", "components": {...}}`.
    """

    status: Literal["available", "unavailable"]
    components: dict[str, ComponentStatus]


class ReadinessCheck(Protocol):
    """One dependency probe; `name` keys the component map in the report."""

    @property
    def name(self) -> str: ...

    async def check(self) -> bool: ...


class ReadinessRegistry:
    """Ordered component probes rendered into one ReadinessReport."""

    def __init__(self, checks: Sequence[ReadinessCheck]) -> None:
        self._checks: tuple[ReadinessCheck, ...] = tuple(checks)

    async def check(self) -> ReadinessReport:
        components: dict[str, ComponentStatus] = {}
        for probe in self._checks:
            try:
                available = await probe.check()
            except Exception:
                # A failing probe (connection refused, timeout, ...) means
                # the component is down, not that the endpoint errors.
                available = False
            components[probe.name] = "ok" if available else "down"
        ready = all(status == "ok" for status in components.values())
        report_status: Literal["available", "unavailable"] = (
            "available" if ready else "unavailable"
        )
        return ReadinessReport(status=report_status, components=components)


class PostgresReadinessCheck:
    """`SELECT 1` through the process-wide engine (spec §34: reachable)."""

    name = "postgres"

    async def check(self) -> bool:
        engine = get_async_engine()
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:
            return False
        return True


class RedisReadinessCheck:
    """`PING` against the configured Redis URL (spec §34: reachable)."""

    name = "redis"

    async def check(self) -> bool:
        client = aioredis.from_url(get_settings().redis_url)
        try:
            return await client.ping()
        except Exception:
            return False
        finally:
            await client.aclose()


@lru_cache
def get_readiness_registry() -> ReadinessRegistry:
    """Process-wide default registry: PostgreSQL + Redis (see module docstring
    for why S3 and the messaging providers are excluded)."""
    return ReadinessRegistry([PostgresReadinessCheck(), RedisReadinessCheck()])
