# backend/tests/unit/core/test_config.py
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db/test")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("S3_BUCKET", "campusquest")
    monkeypatch.setenv("S3_ACCESS_KEY", "access")
    monkeypatch.setenv("S3_SECRET_KEY", "secret")
    monkeypatch.setenv("BUSINESS_TIMEZONE", "Asia/Shanghai")


def test_business_timezone_defaults_to_configured_value(monkeypatch):
    _set_required_env(monkeypatch)
    settings = Settings()
    assert settings.business_timezone == "Asia/Shanghai"


def test_missing_database_url_fails(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_invalid_business_timezone_fails(monkeypatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("BUSINESS_TIMEZONE", "Mars/Olympus_Mons")
    with pytest.raises(ValidationError, match="valid IANA timezone"):
        Settings()


def test_optional_settings_have_documented_defaults(monkeypatch) -> None:
    _set_required_env(monkeypatch)
    settings = Settings()
    assert settings.access_token_ttl_minutes == 15
    assert settings.refresh_token_ttl_days == 30
    assert settings.max_upload_bytes_default == 200 * 1024 * 1024


def test_development_accepts_sentinel_otp_hmac_secret(monkeypatch) -> None:
    # The committed development-only sentinel stays usable locally without
    # any extra configuration.
    _set_required_env(monkeypatch)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("OTP_HMAC_SECRET", raising=False)
    settings = Settings()
    assert settings.environment == "development"
    assert settings.otp_hmac_secret == "dev-only-insecure-otp-hmac-secret"


def test_production_rejects_sentinel_otp_hmac_secret(monkeypatch) -> None:
    # With the known sentinel, anyone who can read Redis also knows the HMAC
    # key and can brute-force the 10^6 OTP space offline (spec §33.2), so a
    # production deployment must fail fast at settings load instead.
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("OTP_HMAC_SECRET", raising=False)
    with pytest.raises(ValidationError, match="OTP_HMAC_SECRET"):
        Settings()


def test_production_accepts_overridden_otp_hmac_secret(monkeypatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("OTP_HMAC_SECRET", "a-real-deployment-secret")
    settings = Settings()
    assert settings.environment == "production"
    assert settings.otp_hmac_secret == "a-real-deployment-secret"


def test_environment_rejects_unknown_values(monkeypatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    with pytest.raises(ValidationError):
        Settings()


def test_get_settings_reads_environment_and_caches(monkeypatch) -> None:
    _set_required_env(monkeypatch)
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.database_url == "postgresql+asyncpg://u:p@db/test"
        assert settings.redis_url == "redis://redis:6379/0"
        assert settings.s3_endpoint_url == "http://minio:9000"
        assert settings.s3_bucket == "campusquest"
        assert settings.business_timezone == "Asia/Shanghai"
        assert get_settings() is settings
    finally:
        get_settings.cache_clear()
