# backend/tests/unit/identity/test_provider_composition.py
"""The identity composition root resolves senders from settings (PR #2
hardening P0-2, fail-closed).

The chain: Settings owns the typed provider fields; production refuses
provider="logging" at Settings construction (pinned in
tests/unit/core/test_config.py), so the wiring below can never hand a
no-send adapter to a production request. development explicitly runs
the logging adapters, whose "logging:"-prefixed receipts mark recorded
deliveries as simulated.
"""

from __future__ import annotations

from app.core.config import Settings
from app.integrations.email import LoggingEmailSender
from app.integrations.sms import LoggingSmsSender
from app.modules.identity.providers import get_email_sender, get_sms_sender


def _settings() -> Settings:
    # Inert values: nothing connects at composition time.
    return Settings(
        database_url="postgresql+asyncpg://u:p@db/test",
        redis_url="redis://redis:6379/0",
        s3_endpoint_url="http://minio:9000",
        s3_bucket="campusquest",
        s3_access_key="access",
        s3_secret_key="secret",
        business_timezone="Asia/Shanghai",
    )


def test_sender_providers_read_settings_not_hardcoded() -> None:
    settings = _settings()
    assert settings.sms_provider == "logging"
    assert settings.email_provider == "logging"
    assert isinstance(get_sms_sender(settings=settings), LoggingSmsSender)
    assert isinstance(get_email_sender(settings=settings), LoggingEmailSender)
