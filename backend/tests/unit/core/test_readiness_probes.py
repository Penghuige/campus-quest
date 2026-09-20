# backend/tests/unit/core/test_readiness.py
"""Probe boundedness (spec §34): /health/ready must report `down` quickly
against a wedged dependency instead of hanging the endpoint.

The silent TCP listener (bind + listen, never accept, never respond) is a
peer indistinguishable from a black-holed dependency and works on any
machine, unlike a non-routable IP that some sandboxes reject instantly.
Test-level `asyncio.wait_for` bounds turn "the probe hangs forever" into a
plain test failure, which is exactly the pre-fix behavior.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

import app.core.readiness as readiness
from app.core.config import Settings
from app.core.readiness import PostgresReadinessCheck, RedisReadinessCheck


def _settings_with(redis_url: str) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://u:p@localhost:5432/none",
        redis_url=redis_url,
        s3_endpoint_url="http://localhost:9000",
        s3_bucket="campusquest",
        s3_access_key="access",
        s3_secret_key="secret",
        business_timezone="Asia/Shanghai",
    )


@contextmanager
def _silent_tcp_listener() -> Iterator[int]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        yield server.getsockname()[1]
    finally:
        server.close()


def test_probe_budget_is_about_two_seconds() -> None:
    assert readiness.READINESS_PROBE_TIMEOUT_SECONDS == 2.0


async def test_redis_probe_passes_socket_timeouts_to_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _StubRedis:
        async def ping(self) -> bool:
            return True

        async def aclose(self) -> None:
            return None

    def fake_from_url(url: str, **kwargs: object) -> _StubRedis:
        captured.clear()
        captured.update(kwargs)
        return _StubRedis()

    monkeypatch.setattr(readiness.aioredis, "from_url", fake_from_url)
    monkeypatch.setattr(
        readiness, "get_settings", lambda: _settings_with("redis://h:1/0")
    )

    assert await RedisReadinessCheck().check() is True
    assert (
        captured["socket_connect_timeout"] == readiness.READINESS_PROBE_TIMEOUT_SECONDS
    )
    assert captured["socket_timeout"] == readiness.READINESS_PROBE_TIMEOUT_SECONDS


async def test_redis_probe_reports_down_quickly_against_silent_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _silent_tcp_listener() as port:
        monkeypatch.setattr(
            readiness,
            "get_settings",
            lambda: _settings_with(f"redis://127.0.0.1:{port}/0"),
        )
        started = time.monotonic()
        result = await asyncio.wait_for(RedisReadinessCheck().check(), timeout=8)
        elapsed = time.monotonic() - started

    assert result is False
    # Tighter than the ~5s redis-py default: proves OUR 2s budget applies.
    assert elapsed < 4


async def test_postgres_probe_reports_down_quickly_against_silent_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _silent_tcp_listener() as port:
        engine = create_async_engine(f"postgresql+asyncpg://u:p@127.0.0.1:{port}/test")
        monkeypatch.setattr(readiness, "get_async_engine", lambda: engine)
        try:
            started = time.monotonic()
            result = await asyncio.wait_for(PostgresReadinessCheck().check(), timeout=8)
            elapsed = time.monotonic() - started
        finally:
            await engine.dispose()

    assert result is False
    assert elapsed < 8


async def test_postgres_probe_wait_for_bounds_a_hung_probe() -> None:
    # A probe that never returns (wedged server after connect) must be
    # cancelled at the probe budget, not stall /health/ready.
    class _HungProbe(PostgresReadinessCheck):
        async def _probe(self) -> None:
            await asyncio.sleep(30)

    started = time.monotonic()
    assert await _HungProbe().check() is False
    assert time.monotonic() - started < 5
