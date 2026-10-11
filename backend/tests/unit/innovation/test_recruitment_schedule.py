"""Thirty actual days, with one catch-up and no automatic closure."""

from datetime import UTC, datetime, timedelta

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
