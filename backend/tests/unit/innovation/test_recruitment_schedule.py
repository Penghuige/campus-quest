"""Thirty actual days, with one catch-up and no automatic closure."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.modules.innovation.recruitment_scheduler import next_reminder_due


@pytest.mark.parametrize("elapsed,advance", [(0, 30), (29, 30), (30, 60), (95, 120)])
def test_next_due_preserves_original_series(elapsed, advance):
    due = datetime(2026, 10, 11, tzinfo=UTC)
    assert next_reminder_due(due, due + timedelta(days=elapsed)) == due + timedelta(
        days=advance
    )


def test_before_due_does_not_advance():
    due = datetime(2026, 10, 11, tzinfo=UTC)
    assert next_reminder_due(due, due - timedelta(milliseconds=1)) == due


@pytest.mark.parametrize(
    "key",
    [
        "missing",
        "wrong:a:b:2026-10-11T00:00:00Z",
        "ie-recruitment:invalid:invalid:2026-10-11T00:00:00Z",
    ],
)
def test_malformed_reminder_identity_is_rejected(key):
    from app.modules.notifications.delivery_service import _recruitment_reminder_ids

    with pytest.raises(ValueError):
        _recruitment_reminder_ids(key)


def test_reminder_identity_requires_aware_due_and_preserves_uuid_scope():
    from app.modules.notifications.delivery_service import _recruitment_reminder_ids

    recruitment, cycle = uuid4(), uuid4()
    prefix = f"ie-recruitment:{recruitment}:{cycle}:"
    with pytest.raises(ValueError, match="timezone aware"):
        _recruitment_reminder_ids(prefix + "2026-10-11T00:00:00")
    assert _recruitment_reminder_ids(prefix + "2026-10-11T00:00:00Z") == (
        recruitment,
        cycle,
    )
