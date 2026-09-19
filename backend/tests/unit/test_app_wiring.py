# backend/tests/unit/test_app_wiring.py
"""API-process wiring of the shared Celery app.

In the API process nothing else constructs a Celery app, so unless
`create_app()` does it at startup, `shared_task` proxies used by request
handlers (later plans enqueue deadline/validation jobs) would bind to
Celery's fallback default app and its `amqp://guest@localhost` broker
instead of the settings-configured Redis URL.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.workers.celery_app import get_celery_app


def _set_required_env(monkeypatch: pytest.MonkeyPatch, redis_url: str) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db/test")
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("S3_BUCKET", "campusquest")
    monkeypatch.setenv("S3_ACCESS_KEY", "access")
    monkeypatch.setenv("S3_SECRET_KEY", "secret")
    monkeypatch.setenv("BUSINESS_TIMEZONE", "Asia/Shanghai")


def test_lifespan_binds_celery_current_app_to_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A broker host no default would ever use proves the app came from the
    # settings in THIS process, not from Celery's default configuration.
    expected = "redis://api-celery-bind-check:6379/3"
    _set_required_env(monkeypatch, redis_url=expected)
    get_settings.cache_clear()
    get_celery_app.cache_clear()
    try:
        # Entering the TestClient context runs the create_app lifespan.
        with TestClient(create_app()):
            pass

        from celery import current_app

        assert current_app.main == "campusquest"
        assert current_app.conf.broker_url == expected
        assert current_app.conf.result_backend == expected
        assert "amqp" not in current_app.conf.broker_url
        assert "guest" not in current_app.conf.broker_url
        assert current_app._get_current_object() is get_celery_app()
    finally:
        # Repopulate lazily from the restored environment for later tests.
        get_settings.cache_clear()
        get_celery_app.cache_clear()
