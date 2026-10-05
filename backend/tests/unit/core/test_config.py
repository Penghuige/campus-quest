# backend/tests/unit/core/test_config.py
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
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


def _set_production_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real values for the three committed dev-only sentinels, so a
    production-environment test fails on the rule under test, not on the
    (already-pinned) secret rejection."""
    monkeypatch.setenv("OTP_HMAC_SECRET", "a-real-deployment-secret")
    monkeypatch.setenv("TOKEN_SECRET", "a-real-access-token-secret-0123456789")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode())


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
    assert settings.staff_invitation_ttl_hours == 48
    assert settings.email_verification_token_ttl_hours == 24


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
    # Since Task 5 production refuses EVERY committed sentinel, so the
    # token-signing and TOTP-encryption secrets must be real here too.
    monkeypatch.setenv("TOKEN_SECRET", "a-real-access-token-secret-0123456789")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    # The secret rule itself now passes: with real secrets the startup
    # failure no longer names the secrets — V1 production still fails
    # closed, but on the logging provider (see the provider tests below;
    # real SMS/EMAIL adapters land with the provider project).
    with pytest.raises(ValidationError, match="SMS_PROVIDER") as exc_info:
        Settings()
    assert "OTP_HMAC_SECRET" not in str(exc_info.value)


def test_development_accepts_sentinel_token_secret(monkeypatch) -> None:
    # The committed development-only sentinel stays usable locally without
    # any extra configuration.
    _set_required_env(monkeypatch)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("TOKEN_SECRET", raising=False)
    settings = Settings()
    assert settings.environment == "development"
    assert settings.token_secret == "dev-only-insecure-access-token-secret"


def test_production_rejects_sentinel_token_secret(monkeypatch) -> None:
    # With the known sentinel anyone can forge access tokens (HS256 signing
    # key public), so a production deployment must fail fast at settings
    # load instead (spec §5.6, backend-engineering §17).
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("OTP_HMAC_SECRET", "a-real-deployment-secret")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("TOKEN_SECRET", raising=False)
    with pytest.raises(ValidationError, match="TOKEN_SECRET"):
        Settings()


def test_short_token_secret_rejected_everywhere(monkeypatch) -> None:
    # RFC 7518 §3.2 wants >= 32 bytes for HS256; a short explicit secret
    # fails at settings load in any environment, not just production.
    _set_required_env(monkeypatch)
    monkeypatch.setenv("TOKEN_SECRET", "too-short-token-secret")
    with pytest.raises(ValidationError, match="at least 32"):
        Settings()


def test_production_accepts_overridden_token_secret(monkeypatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("OTP_HMAC_SECRET", "a-real-deployment-secret")
    monkeypatch.setenv("TOKEN_SECRET", "a-real-access-token-secret-0123456789")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    # Same as the OTP-HMAC override above: the token-secret rule passes,
    # and the remaining startup failure is the (V1-unavoidable) logging
    # provider, not the secrets.
    with pytest.raises(ValidationError, match="EMAIL_PROVIDER") as exc_info:
        Settings()
    assert "TOKEN_SECRET" not in str(exc_info.value)


def test_environment_rejects_unknown_values(monkeypatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    with pytest.raises(ValidationError):
        Settings()


# --- outbound providers (PR #2 hardening P0-2, fail-closed) ------------------------


def test_providers_default_to_logging_in_development(monkeypatch) -> None:
    # development MAY run explicitly on the logging provider: sends are
    # simulated there, and the recorded deliveries carry "logging:"-prefixed
    # provider_message_ids so the simulation is distinguishable from real
    # provider receipts.
    _set_required_env(monkeypatch)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    settings = Settings()
    assert settings.environment == "development"
    assert settings.sms_provider == "logging"
    assert settings.email_provider == "logging"


def test_production_rejects_logging_sms_provider(monkeypatch) -> None:
    # G4/G5: the logging adapter delivers nothing while the delivery row
    # is recorded SENT, so production must fail at settings load instead
    # of running a fake-success provider. Real secrets are set so the
    # failure under test is the provider one.
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    _set_production_secrets(monkeypatch)
    monkeypatch.delenv("SMS_PROVIDER", raising=False)
    with pytest.raises(ValidationError, match="SMS_PROVIDER"):
        Settings()


def test_production_rejects_logging_email_provider(monkeypatch) -> None:
    # Same guard for the email channel: both defaults offend together,
    # and the one error names everything the deployer must configure.
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    _set_production_secrets(monkeypatch)
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    with pytest.raises(ValidationError, match="EMAIL_PROVIDER"):
        Settings()


def test_real_provider_values_are_not_yet_selectable(monkeypatch) -> None:
    # Boundary: real adapters ("twilio"/"smtp") arrive with the provider
    # project; until the Literals grow, pydantic rejects the values at
    # settings load — fail closed rather than accepting a value that no
    # composition point wires.
    _set_required_env(monkeypatch)
    monkeypatch.setenv("SMS_PROVIDER", "twilio")
    with pytest.raises(ValidationError):
        Settings()
    monkeypatch.setenv("SMS_PROVIDER", "logging")
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
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


def test_refresh_grace_bounds_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The replay window changes bearer-replay authority: only the
    owner-approved 0..60 range loads. Negative must not mean "off" by
    accident and an hours-long window must never deploy by typo."""
    from cryptography.fernet import Fernet

    _set_required_env(monkeypatch)
    valid_key = Fernet.generate_key().decode()

    # 0 and the ceiling are legal (0 needs no key).
    monkeypatch.setenv("REFRESH_GRACE_SECONDS", "0")
    Settings()
    monkeypatch.setenv("REFRESH_GRACE_SECONDS", "60")
    monkeypatch.setenv("REFRESH_REPLAY_ENCRYPTION_KEY", valid_key)
    Settings()

    for bad in ("-1", "61", "3600"):
        monkeypatch.setenv("REFRESH_GRACE_SECONDS", bad)
        with pytest.raises(ValidationError):
            Settings()
    monkeypatch.setenv("REFRESH_GRACE_SECONDS", "30")


def test_replay_key_required_and_valid_when_grace_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A window > 0 without a dedicated, structurally valid Fernet key
    fails at config load — not at the first envelope write. The key is
    dedicated: sharing the TOTP key is a wiring error the type system
    cannot catch, so the loader catches shape at least."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("REFRESH_GRACE_SECONDS", "30")

    # missing / empty / not-a-Fernet-key all refuse to load
    for bad in (None, "", "definitely-not-base64-fernet-material"):
        if bad is None:
            monkeypatch.delenv("REFRESH_REPLAY_ENCRYPTION_KEY", raising=False)
        else:
            monkeypatch.setenv("REFRESH_REPLAY_ENCRYPTION_KEY", bad)
        with pytest.raises(ValidationError):
            Settings()

    from cryptography.fernet import Fernet

    monkeypatch.setenv("REFRESH_REPLAY_ENCRYPTION_KEY", Fernet.generate_key().decode())
    settings = Settings()
    assert settings.refresh_grace_seconds == 30

    # grace off: the key is ignored entirely
    monkeypatch.setenv("REFRESH_GRACE_SECONDS", "0")
    monkeypatch.setenv("REFRESH_REPLAY_ENCRYPTION_KEY", "garbage")
    assert Settings().refresh_grace_seconds == 0


def _set_aliyun_sms_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIYUN_SMS_ACCESS_KEY_ID", "test-key-id")
    monkeypatch.setenv("ALIYUN_SMS_ACCESS_KEY_SECRET", "test-key-secret")
    monkeypatch.setenv("ALIYUN_SMS_SIGN_NAME", "测试签名")
    monkeypatch.setenv("ALIYUN_SMS_OTP_TEMPLATE_CODE", "100001")


def test_aliyun_dypns_is_selectable_in_development(monkeypatch) -> None:
    # The first real SMS adapter: selectable with the four config
    # values set; endpoint/region carry deployment defaults.
    _set_required_env(monkeypatch)
    monkeypatch.setenv("SMS_PROVIDER", "aliyun_dypns")
    _set_aliyun_sms_config(monkeypatch)
    settings = Settings()
    assert settings.sms_provider == "aliyun_dypns"
    assert settings.aliyun_sms_endpoint == "dypnsapi.aliyuncs.com"
    assert settings.aliyun_sms_region == "ap-southeast-1"


def test_production_rejects_aliyun_dypns_with_incomplete_config(
    monkeypatch,
) -> None:
    # The fail-closed chain extends: a REAL provider selection with
    # missing config values must fail at startup (the specific gate
    # fires before the general logging refusal, naming exactly what
    # to set).
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    _set_production_secrets(monkeypatch)
    monkeypatch.setenv("SMS_PROVIDER", "aliyun_dypns")
    monkeypatch.delenv("ALIYUN_SMS_ACCESS_KEY_ID", raising=False)
    with pytest.raises(ValidationError, match="ALIYUN_SMS_ACCESS_KEY_ID"):
        Settings()

    # Complete config clears this gate; production then still fails on
    # the EMAIL logging provider (no real email adapter exists yet —
    # the correct V1 posture), proving the gates layer independently.
    _set_aliyun_sms_config(monkeypatch)
    with pytest.raises(ValidationError, match="EMAIL_PROVIDER"):
        Settings()


def test_sms_factory_refuses_aliyun_with_incomplete_config(monkeypatch) -> None:
    # Composition-time refusal in EVERY environment (not only
    # production): a half-configured real provider never reaches a
    # first send signed with garbage.
    from app.integrations.sms import build_sms_sender

    _set_required_env(monkeypatch)
    monkeypatch.setenv("SMS_PROVIDER", "aliyun_dypns")
    monkeypatch.delenv("ALIYUN_SMS_ACCESS_KEY_ID", raising=False)
    settings = Settings()
    with pytest.raises(LookupError, match="aliyun_sms_access_key_id"):
        build_sms_sender("aliyun_dypns", settings)

    _set_aliyun_sms_config(monkeypatch)
    settings = Settings()
    sender = build_sms_sender("aliyun_dypns", settings)
    assert type(sender).__name__ == "AliyunDypnsSmsSender"
