# backend/tests/unit/db/test_session.py
"""Unit tests for async engine/session creation and request-scope semantics."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

import app.db.session as db_session_module
from app.core.config import get_settings
from app.db.session import create_db_engine, get_async_session_maker, get_db_session


class _RecordingSession:
    """Test double standing in for AsyncSession; records lifecycle calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def commit(self) -> None:
        self.calls.append("commit")

    async def rollback(self) -> None:
        self.calls.append("rollback")

    async def close(self) -> None:
        self.calls.append("close")


def _set_required_env(monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
    # Complete required-settings set: these tests must pass when only
    # tests/unit + tests/workers are collected, i.e. without the integration
    # conftest having installed its env defaults in the same process.
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setenv("S3_BUCKET", "campusquest-test")
    monkeypatch.setenv("S3_ACCESS_KEY", "campusquest")
    monkeypatch.setenv("S3_SECRET_KEY", "campusquest-dev")
    monkeypatch.setenv("BUSINESS_TIMEZONE", "Asia/Shanghai")


def _reset_caches() -> None:
    get_settings.cache_clear()
    db_session_module.get_async_engine.cache_clear()
    db_session_module.get_async_session_maker.cache_clear()


async def test_create_db_engine_reads_settings_url_and_enables_pre_ping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = "postgresql+asyncpg://u:p@localhost:5432/campusquest_test"
    _set_required_env(monkeypatch, database_url)
    _reset_caches()
    try:
        engine = create_db_engine(get_settings())
        assert engine.url.render_as_string(hide_password=False) == database_url
        assert engine.sync_engine.pool._pre_ping is True
        await engine.dispose()
    finally:
        _reset_caches()


async def test_get_async_session_maker_binds_cached_engine_without_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_env(
        monkeypatch, "postgresql+asyncpg://u:p@localhost:5432/campusquest_test"
    )
    _reset_caches()
    try:
        engine = db_session_module.get_async_engine()
        maker = get_async_session_maker()
        assert maker.kw["bind"] is engine
        assert maker.kw["expire_on_commit"] is False
    finally:
        _reset_caches()


async def test_get_db_session_commits_and_closes_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _RecordingSession()
    monkeypatch.setattr(
        db_session_module, "get_async_session_maker", lambda: lambda: stub
    )

    generator: AsyncIterator[Any] = get_db_session()
    session = await generator.__anext__()
    assert session is stub
    with pytest.raises(StopAsyncIteration):
        await generator.__anext__()

    assert stub.calls == ["commit", "close"]


async def test_get_db_session_rolls_back_and_closes_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _RecordingSession()
    monkeypatch.setattr(
        db_session_module, "get_async_session_maker", lambda: lambda: stub
    )

    generator: AsyncIterator[Any] = get_db_session()
    await generator.__anext__()
    with pytest.raises(RuntimeError, match="boom"):
        await generator.athrow(RuntimeError("boom"))

    assert stub.calls == ["rollback", "close"]
