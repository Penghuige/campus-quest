"""Unit tests for the loadtest env module (defaults + env overrides)."""

import pytest

from loadtest.env import LoadtestEnv, loadtest_env


def test_loadtest_env_defaults_to_local_compose_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CQ_LOADTEST_API_BASE", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    env = loadtest_env()

    assert env.api_base == "http://localhost:8000/api/v1"
    assert env.database_url == (
        "postgresql+asyncpg://test:test@localhost:15432/campusquest_test"
    )


def test_loadtest_env_honors_exported_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CQ_LOADTEST_API_BASE", "http://other-host:9/api/v1")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u@h:5/db")

    env = loadtest_env()

    assert isinstance(env, LoadtestEnv)
    assert env.api_base == "http://other-host:9/api/v1"
    assert env.database_url == "postgresql+asyncpg://u@h:5/db"
