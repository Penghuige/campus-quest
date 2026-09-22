# backend/tests/unit/core/test_cleanup_lease_invariant.py
"""The machine-checked lease > worst-case-delete-budget invariant (final
merge-readiness review P0, Option A): the fencing token fences a stale
worker's LATE DB WRITES, but no token can revoke an already-sent S3
DELETE — takeover + release may only open protection once every possible
predecessor external call is provably finished. The proof is this
invariant: the lease strictly outlasts the configured worst-case delete
budget, so a predecessor's bounded provider call has terminated before
its lease can expire and any takeover can begin."""

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


def test_default_lease_strictly_exceeds_the_delete_budget(monkeypatch) -> None:
    _env(monkeypatch)
    settings = Settings()
    # Defaults: 2 total attempts x (10 connect + 30 read) x 2 API calls
    # (HEAD+DELETE) + 60s backoff margin = 220s; the 300s lease wins.
    assert settings.s3_worst_case_delete_budget_seconds == 220
    assert settings.cleanup_claim_lease_seconds == 300
    assert settings.cleanup_claim_lease_seconds > (
        settings.s3_worst_case_delete_budget_seconds
    )


def test_lease_equal_to_budget_is_rejected(monkeypatch) -> None:
    # The boundary itself fails: "equal" leaves no room for the
    # predecessor call to be CERTAINLY done at the expiry instant.
    _env(monkeypatch, cleanup_claim_lease_seconds="220")
    with pytest.raises(ValidationError, match="STRICTLY exceed"):
        Settings()


def test_short_lease_is_rejected_with_the_exact_arithmetic(monkeypatch) -> None:
    _env(monkeypatch, cleanup_claim_lease_seconds="60")
    with pytest.raises(ValidationError, match=r"2 total attempts.*HEAD\+DELETE"):
        Settings()


def test_widening_the_timeouts_reevaluates_the_budget(monkeypatch) -> None:
    # The invariant reads the SAME settings the adapter configures the
    # client with — a deployment widening read timeouts forces the lease
    # up or fails construction (no drifting prose arithmetic).
    _env(
        monkeypatch,
        s3_read_timeout_seconds="120",
        cleanup_claim_lease_seconds="220",
    )
    with pytest.raises(ValidationError, match="STRICTLY exceed"):
        Settings()
    _env(
        monkeypatch,
        s3_read_timeout_seconds="120",
        cleanup_claim_lease_seconds="600",
    )
    assert Settings().cleanup_claim_lease_seconds == 600
