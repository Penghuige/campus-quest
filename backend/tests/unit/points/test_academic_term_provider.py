# backend/tests/unit/points/test_academic_term_provider.py
"""The settings-driven academic-term provider (PR #2 hardening, Task 4).

``SettingsAcademicTermProvider`` reads ``Settings.current_academic_term``
(the committed dev default "2026-fall"; deployments turn the term with
CURRENT_ACADEMIC_TERM until Plan 08's audited admin setting). The key is
validated at construction — a misconfigured deployment fails at wiring
time (the StaticAcademicTermProvider ruling carried over). Redemption
snapshot behavior (the term persisted at creation time) is asserted at
the API/service layers: ``tests/integration/points/test_points_api.py``
(``term_key == "2026-fall"`` flows through this provider) and
``test_redemption_concurrency.py``
(``test_term_limit_counts_snapshot_releases_on_reject_and_switches``).
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.modules.points.redemption_service import (
    AcademicTermConfigurationError,
    SettingsAcademicTermProvider,
)


def _settings(current_academic_term: str) -> Settings:
    # The mandatory deployment fields are dummies: the provider reads
    # one field and the unit under test never touches a database.
    return Settings(
        database_url="postgresql+asyncpg://u:p@db/test",
        redis_url="redis://redis:6379/0",
        s3_endpoint_url="http://minio:9000",
        s3_bucket="campusquest",
        s3_access_key="access",
        s3_secret_key="secret",
        business_timezone="Asia/Shanghai",
        current_academic_term=current_academic_term,
    )


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mandatory deployment fields as dummies: the provider reads one
    field and the unit under test never touches a database."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db/test")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("S3_BUCKET", "campusquest")
    monkeypatch.setenv("S3_ACCESS_KEY", "access")
    monkeypatch.setenv("S3_SECRET_KEY", "secret")
    monkeypatch.setenv("BUSINESS_TIMEZONE", "Asia/Shanghai")


def test_provider_returns_the_settings_value() -> None:
    provider = SettingsAcademicTermProvider(_settings("2027-spring"))
    assert provider.current_term_key() == "2027-spring"


def test_default_settings_carry_the_dev_term_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.delenv("CURRENT_ACADEMIC_TERM", raising=False)
    settings = Settings()
    assert settings.current_academic_term == "2026-fall"  # the dev default
    assert SettingsAcademicTermProvider(settings).current_term_key() == "2026-fall"


def test_the_term_is_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deployments turn the term with CURRENT_ACADEMIC_TERM — the one
    non-code place the term moves until Plan 08's audited setting."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("CURRENT_ACADEMIC_TERM", "2027-spring")
    provider = SettingsAcademicTermProvider(Settings())
    assert provider.current_term_key() == "2027-spring"


@pytest.mark.parametrize("bad", ["", "   ", "x" * 65])
def test_unusable_term_fails_at_construction(bad: str) -> None:
    """The wiring-time ruling: a blank or over-width key is a
    configuration failure, refused when the provider is built — never a
    calendar guess at redemption time."""
    with pytest.raises(AcademicTermConfigurationError):
        SettingsAcademicTermProvider(_settings(bad))
