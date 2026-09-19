# backend/tests/unit/core/test_clock.py
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.core.clock import FrozenClock, SystemClock


def test_frozen_clock_returns_exact_aware_instant():
    instant = datetime(2026, 9, 19, 3, 0, tzinfo=UTC)
    assert FrozenClock(instant).now() == instant


def test_frozen_clock_rejects_naive_datetime():
    with pytest.raises(ValueError):
        # Naive on purpose: FrozenClock must reject it.
        FrozenClock(datetime(2026, 9, 19, 3, 0))  # noqa: DTZ001


def test_system_clock_returns_aware_utc_datetime() -> None:
    before = datetime.now(UTC)
    now = SystemClock().now()
    after = datetime.now(UTC)
    assert now.tzinfo is UTC
    assert before <= now <= after


def test_frozen_clock_normalizes_non_utc_aware_datetime_to_utc() -> None:
    shanghai = timezone(timedelta(hours=8))
    instant = datetime(2026, 9, 19, 11, 0, tzinfo=shanghai)
    assert FrozenClock(instant).now() == datetime(2026, 9, 19, 3, 0, tzinfo=UTC)
