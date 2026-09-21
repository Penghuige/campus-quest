# backend/tests/unit/points/test_academic_term_provider.py
"""The academic-term provider family (PR #2 hardening, tasks 4 + 8).

``SettingsAcademicTermProvider`` reads ``Settings.current_academic_term``
(the committed dev default "2026-fall"; the env seed). The key is
validated at construction — a misconfigured deployment fails at wiring
time (the StaticAcademicTermProvider ruling carried over).

``SystemAcademicTermProvider`` (step 8) is the production binding: the
``system_settings`` CURRENT_ACADEMIC_TERM row is the fact, the settings
value is only the bootstrap seed (G7) — row present -> row value; no
row -> seed; a present-but-corrupt row fails LOUDLY (no silent seed
fallback that would quietly freeze every new redemption on a stale
term). The per-request storage read is the composition root's job
(points/router.py, asserted at the API layer in
tests/integration/points/test_term_setting_provider.py); what is pinned
here is the priority and validation RULE.

Redemption snapshot behavior (the term persisted at creation time) is
asserted at the API/service layers: ``tests/integration/points/
test_points_api.py`` (``term_key == "2026-fall"`` flows through the
provider) and ``test_redemption_concurrency.py``
(``test_term_limit_counts_snapshot_releases_on_reject_and_switches``).
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.modules.points.redemption_service import (
    AcademicTermConfigurationError,
    SettingsAcademicTermProvider,
    SystemAcademicTermProvider,
)


def _settings(current_academic_term: str) -> Settings:
    # The mandatory deployment fields are dummies: the providers read
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


# --- SystemAcademicTermProvider (PR #2 hardening step 8) ------------------------------


def test_system_setting_row_is_the_fact_over_the_seed() -> None:
    """G7: a present system_settings row wins over the env seed — the
    row is what the admin configured through the audited API."""
    provider = SystemAcademicTermProvider(
        configured_term="2027-spring", fallback=_settings("2026-fall")
    )
    assert provider.current_term_key() == "2027-spring"


def test_no_row_falls_back_to_the_seed() -> None:
    """First boot, before any admin sets the term: the deployment seed
    (env CURRENT_ACADEMIC_TERM) is the initial value."""
    provider = SystemAcademicTermProvider(
        configured_term=None, fallback=_settings("2026-fall")
    )
    assert provider.current_term_key() == "2026-fall"


@pytest.mark.parametrize("corrupt", ["", "   ", "x" * 65])
def test_corrupt_row_fails_loudly_instead_of_seed_fallback(corrupt: str) -> None:
    """A blank/over-width ROW value (only reachable by direct database
    edits — the admin API validates) is a configuration failure, not a
    cue to silently fall back to the seed: masking it would quietly move
    every new redemption to the stale seed term (the fail-closed G5
    posture; the same ruling as the wiring-time validation)."""
    with pytest.raises(AcademicTermConfigurationError):
        SystemAcademicTermProvider(
            configured_term=corrupt, fallback=_settings("2026-fall")
        )


@pytest.mark.parametrize("corrupt_seed", ["", "   ", "x" * 65])
def test_corrupt_seed_fails_when_it_is_the_answer(corrupt_seed: str) -> None:
    """No row + unusable seed is the same wiring-time configuration
    failure the SettingsAcademicTermProvider has always enforced. With a
    row present the seed is not consulted (masked, not validated — the
    row is the answer)."""
    with pytest.raises(AcademicTermConfigurationError):
        SystemAcademicTermProvider(
            configured_term=None, fallback=_settings(corrupt_seed)
        )


def test_row_value_is_stripped_like_every_term_key() -> None:
    """The provider family's read-time semantics: the term key is the
    stripped value (the redemption service re-validates, but the
    provider's answer is already canonical)."""
    provider = SystemAcademicTermProvider(
        configured_term="  2027-spring  ", fallback=_settings("2026-fall")
    )
    assert provider.current_term_key() == "2027-spring"
