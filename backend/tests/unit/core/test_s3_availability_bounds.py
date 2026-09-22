# backend/tests/unit/core/test_s3_availability_bounds.py
"""The s3_* provider-call settings are AVAILABILITY CONTROLS with sanity
bounds (post-merge P0, P2 hardening) — deliberately NOT a correctness
proof: socket-wait caps are not a request lifetime deadline, so cleanup
correctness comes from claim ownership (provider failures keep the
unfinished claim; see files.cleanup_service), never from wall-clock
arithmetic over these fields."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://t:t@localhost/t")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setenv("S3_BUCKET", "b")
    monkeypatch.setenv("S3_ACCESS_KEY", "k")
    monkeypatch.setenv("S3_SECRET_KEY", "v")
    monkeypatch.setenv("BUSINESS_TIMEZONE", "Asia/Shanghai")
    for key, value in overrides.items():
        monkeypatch.setenv(key.upper(), value)


def test_defaults_are_sane_availability_controls(monkeypatch) -> None:
    _env(monkeypatch)
    settings = Settings()
    assert settings.s3_connect_timeout_seconds == 10
    assert settings.s3_read_timeout_seconds == 30
    assert settings.s3_delete_total_attempts == 2


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("s3_connect_timeout_seconds", "0"),
        ("s3_connect_timeout_seconds", "301"),
        ("s3_read_timeout_seconds", "0"),
        ("s3_read_timeout_seconds", "301"),
        ("s3_delete_total_attempts", "0"),
        ("s3_delete_total_attempts", "11"),
    ],
)
def test_out_of_bounds_values_fail(monkeypatch, field: str, bad: str) -> None:
    _env(monkeypatch, **{field: bad})
    with pytest.raises(ValidationError, match="must be within"):
        Settings()


def test_lease_and_timeouts_are_independent_configuration(monkeypatch) -> None:
    # No cross-field wall-clock invariant ties the lease to the
    # provider settings anymore (the post-merge P0 removed it as an
    # unsound proof): both may be tuned independently, because
    # correctness rests on claim ownership, not timing.
    _env(monkeypatch, cleanup_claim_lease_seconds="60")
    assert Settings().cleanup_claim_lease_seconds == 60
