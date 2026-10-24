# backend/tests/unit/core/test_app_docs_gate.py
"""The production API-docs gate is a hardening invariant (D-3), not a
convention: a merged branch that rebuilds create_app() without the
docs-disabled posture must fail here, not in production (the upstream
innovation branch silently dropped it once — this test pins it shut).

create_app() is exercised through a model_copy of the validated
settings with only ``environment`` flipped: the production *settings*
guard has its own coverage; this file pins the assembly decision.
"""

import pytest

from app.core.config import get_settings

_SETTINGS_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://test:test@localhost:15432/none",
    "REDIS_URL": "redis://localhost:6379/0",
    "S3_ENDPOINT_URL": "http://localhost:9000",
    "S3_BUCKET": "docs-gate-test",
    "S3_ACCESS_KEY": "docs-gate",
    "S3_SECRET_KEY": "docs-gate",
    "BUSINESS_TIMEZONE": "Asia/Shanghai",
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch):
    for key, value in _SETTINGS_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_production_closes_every_docs_surface(monkeypatch: pytest.MonkeyPatch):
    import app.main as main

    production = get_settings().model_copy(update={"environment": "production"})
    monkeypatch.setattr(main, "get_settings", lambda: production)
    app = main.create_app()
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None


def test_development_keeps_docs():
    import app.main as main

    app = main.create_app()
    assert app.docs_url == "/docs"
    assert app.redoc_url == "/redoc"
    assert app.openapi_url == "/openapi.json"
