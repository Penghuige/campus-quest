# backend/tests/unit/submissions/test_upload_service.py
"""UploadService construction and Settings wiring (PR #2 hardening
pass 5c).

The strict ``upload_url_ttl < intent_ttl`` ordering is a deployment
invariant the orphan-intent cleanup depends on (files/cleanup_service
deletes expired intents' objects on the assumption that no legal PUT
can land past expiry — true only under this ordering), so:

- the constructor refuses a violating pair (inverted OR equal) with a
  loud ``ValueError`` before the service exists;
- the committed defaults satisfy the invariant;
- the typed Settings fields carry the documented 600/900 defaults;
- the composition root passes the Settings pair through, so a
  misconfigured deployment fails at the wiring point (the same
  ``ValueError``).

The end-to-end proof that a GOOD Settings pair reaches the issued
grant (URL and intent expiries) lives in
tests/integration/submissions/test_upload_finalize.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import FrozenClock
from app.core.config import Settings
from app.modules.submissions.router import get_upload_service
from app.modules.submissions.upload_service import (
    DEFAULT_INTENT_TTL,
    DEFAULT_UPLOAD_URL_TTL,
    UploadService,
)
from tests.fakes.integrations import FakeObjectStorage

_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


def _settings(**overrides: object) -> Settings:
    """A hermetic Settings (no environment reads): required fields as
    inert values, TTL fields overridable per test."""
    fields: dict[str, object] = {
        "database_url": "postgresql+asyncpg://u:p@db/test",
        "redis_url": "redis://redis:6379/0",
        "s3_endpoint_url": "http://minio:9000",
        "s3_bucket": "campusquest",
        "s3_access_key": "access",
        "s3_secret_key": "secret",
        "business_timezone": "Asia/Shanghai",
    }
    fields.update(overrides)
    return Settings(**fields)  # type: ignore[arg-type]


def _service(upload_url_ttl: timedelta, intent_ttl: timedelta) -> UploadService:
    return UploadService(
        clock=FrozenClock(_NOW),
        storage=FakeObjectStorage(),
        upload_url_ttl=upload_url_ttl,
        intent_ttl=intent_ttl,
    )


@pytest.mark.parametrize(
    ("upload_url_ttl", "intent_ttl"),
    [
        (timedelta(minutes=15), timedelta(minutes=10)),  # inverted
        (timedelta(minutes=10), timedelta(minutes=10)),  # equal: no live
        # finalize window past the intent, and the cleanup's
        # "no legal PUT past expiry" claim loses its boundary margin
    ],
)
def test_constructor_rejects_url_ttl_not_strictly_below_intent_ttl(
    upload_url_ttl: timedelta, intent_ttl: timedelta
) -> None:
    """A URL TTL at or above the intent TTL would let a legal PUT land
    after the orphan-intent cleanup deleted the object — the
    constructor fails loud instead of arming that deployment."""
    with pytest.raises(ValueError, match="strictly shorter"):
        _service(upload_url_ttl, intent_ttl)


def test_constructor_accepts_an_ordered_pair() -> None:
    service = _service(timedelta(minutes=10), timedelta(minutes=15))
    assert service is not None


def test_committed_defaults_satisfy_the_invariant() -> None:
    assert DEFAULT_UPLOAD_URL_TTL < DEFAULT_INTENT_TTL


def test_settings_default_ttls_are_the_documented_pair() -> None:
    settings = _settings()
    assert settings.upload_url_ttl_seconds == 600
    assert settings.upload_intent_ttl_seconds == 900


def test_composition_root_fails_loud_on_a_misconfigured_settings_pair() -> None:
    """The wiring point reads both Settings fields and hands them to
    the constructor, so a violating deployment pair surfaces as the
    constructor's ValueError at get_upload_service — never as a
    silently mis-ordered service."""
    with pytest.raises(ValueError, match="strictly shorter"):
        get_upload_service(
            clock=FrozenClock(_NOW),
            settings=_settings(
                upload_url_ttl_seconds=900, upload_intent_ttl_seconds=600
            ),
            storage=FakeObjectStorage(),
            dispatcher=None,
        )
