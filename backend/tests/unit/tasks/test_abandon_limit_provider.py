# backend/tests/unit/tasks/test_abandon_limit_provider.py
"""``SystemDailyAbandonLimit``'s resolution matrix and the
``AbandonService`` limit-injection contract (PR #5 final review fix B):
row-over-seed priority, 0's DISABLED legality, and the fail-loudly
rulings on corrupt rows and miswired providers — the branches the HTTP
composition suite cannot reach (the settings API normalizes, so
corruption is direct-database-only)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.clock import FrozenClock
from app.modules.identity.events import InMemoryEventCollector
from app.modules.tasks.abandon_service import (
    DAILY_ABANDON_LIMIT,
    AbandonService,
    SystemDailyAbandonLimit,
)

_T0 = datetime(2026, 1, 15, 17, 0, tzinfo=UTC)


def test_no_row_answers_the_settings_seed() -> None:
    """G7: 无行 -> the Settings fallback verbatim (the audited row is the
    fact, the seed only boots a deployment)."""
    assert SystemDailyAbandonLimit(None, 3)() == 3


def test_row_value_replaces_the_seed_including_zero() -> None:
    """A present row IS the cap: decimal text parses, and 0 stays 0 — the
    DISABLED semantics (with the cap at 0 even the day's first abandon
    is over it)."""
    assert SystemDailyAbandonLimit("5", 2)() == 5
    assert SystemDailyAbandonLimit("0", 2)() == 0


@pytest.mark.parametrize(
    ("configured_value", "fallback"),
    [("abc", 2), (None, -1), ("-2", 2)],
)
def test_corrupt_row_or_seed_fails_loudly(
    configured_value: str | None, fallback: int
) -> None:
    """Non-decimal row text or a negative value (either side) raises at
    construction — silently falling back would re-allow abandons the
    Admin believes they disabled."""
    with pytest.raises(ValueError):
        SystemDailyAbandonLimit(configured_value, fallback)


def test_service_accepts_int_zero_and_callable_providers() -> None:
    """The injection contract: a plain int may now be 0 (the registry's
    DISABLED semantics — the old >= 1 refusal could not express it), a
    callable provider defers to ``_daily_limit_now``, and only a
    negative int fails at construction."""
    clock = FrozenClock(_T0)

    def _service(limit: Any) -> AbandonService:
        return AbandonService(
            clock=clock,
            business_timezone="Asia/Shanghai",
            events=InMemoryEventCollector(),
            daily_abandon_limit=limit,
        )

    assert _service(DAILY_ABANDON_LIMIT)._daily_limit_now() == 2
    assert _service(0)._daily_limit_now() == 0
    assert _service(SystemDailyAbandonLimit("7", 2))._daily_limit_now() == 7
    with pytest.raises(ValueError):
        _service(-1)
    # A miswired provider passes construction and fails at RESOLUTION,
    # where its answer is judged.
    miswired = _service(lambda: -5)
    with pytest.raises(ValueError):
        miswired._daily_limit_now()
