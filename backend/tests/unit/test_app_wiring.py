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


def test_create_app_wires_role_bearer_to_identity_actor() -> None:
    # The core role-guard seam (app/core/rbac.py) stays unwired by design
    # until the composition root installs identity's `get_actor` through
    # FastAPI's dependency_overrides — the exact wiring the account-status
    # integration test previews. An unwired guard must fail loudly (500),
    # so this asserts create_app installs it, once, at startup.
    from app.core import rbac
    from app.modules.identity.dependencies import get_actor

    app = create_app()

    assert app.dependency_overrides[rbac.get_role_bearer] is get_actor


def test_create_app_mounts_identity_routes_under_api_v1() -> None:
    # Spec §28: every endpoint lives under /api/v1. The identity API is the
    # first router; the assertion pins the prefix and the core auth surface
    # so a dropped include_router cannot pass silently. Paths are read from
    # the OpenAPI schema (a public, stable surface): this FastAPI version
    # keeps included routers as lazy `_IncludedRouter` nodes, so walking
    # `app.routes` would depend on private flattening internals.
    app = create_app()
    paths = set(app.openapi()["paths"])

    expected = {
        "/api/v1/auth/phone/challenges",
        "/api/v1/auth/phone/challenges/{challenge_id}/verify",
        "/api/v1/auth/register",
        "/api/v1/auth/login",
        "/api/v1/auth/staff/login",
        "/api/v1/auth/refresh",
        "/api/v1/auth/logout",
        "/api/v1/auth/password/forgot",
        "/api/v1/auth/password/reset",
        "/api/v1/auth/staff/invitations/accept",
        "/api/v1/staff/totp/begin",
        "/api/v1/staff/totp/confirm",
        "/api/v1/me",
        "/api/v1/me/nickname",
        "/api/v1/me/phone/change",
        "/api/v1/me/phone/change/confirm",
        "/api/v1/me/email",
        "/api/v1/me/email/verify",
        "/api/v1/me/email/unbind",
        "/api/v1/me/password",
    }
    assert expected <= paths
    assert not any(
        path.startswith("/api/") and not path.startswith("/api/v1/") for path in paths
    )


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
