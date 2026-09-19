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

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, Protocol

import redis.asyncio as aioredis
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import get_async_engine

ComponentStatus = Literal["ok", "down"]

# Probe budget (seconds): a wedged dependency must report `down` within this
# bound instead of stalling /health/ready (spec §34 wants dependency state
# judgeable, and orchestrators themselves timeout far later than 2s).
READINESS_PROBE_TIMEOUT_SECONDS = 2.0


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
    """`SELECT 1` through the process-wide engine (spec §34: reachable).

    The whole probe — engine construction, TCP connect, and the scalar —
    runs under one `asyncio.wait_for` bound: asyncpg has no default connect
    timeout, so without the wrapper a black-holed database would hang the
    ready endpoint until the OS TCP timeout (~minutes). Everything,
    including a settings failure, reports `down` instead of raising.
    """

    name = "postgres"

    async def check(self) -> bool:
        try:
            await asyncio.wait_for(
                self._probe(), timeout=READINESS_PROBE_TIMEOUT_SECONDS
            )
        except Exception:
            return False
        return True

    async def _probe(self) -> None:
        engine = get_async_engine()
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))


class RedisReadinessCheck:
    """`PING` against the configured Redis URL (spec §34: reachable).

    The client is built with both socket timeouts set to the probe budget,
    bounding connect and reply even though redis-py ships its own (slower)
    defaults.
    """

    name = "redis"

    async def check(self) -> bool:
        client = aioredis.from_url(
            get_settings().redis_url,
            socket_connect_timeout=READINESS_PROBE_TIMEOUT_SECONDS,
            socket_timeout=READINESS_PROBE_TIMEOUT_SECONDS,
        )
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
