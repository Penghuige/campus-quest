# backend/tests/unit/points/test_task_reward_amount_guard.py
"""The task-reward entry's positive-amount gate (spec §14/§15; mutation
M10 pin).

``LedgerService.grant_assignment_reward`` is the ASSIGNMENT_REWARD
entry point, and its FIRST statement rejects ``amount <= 0`` with the
typed ``InvalidLedgerEntryError("amount", "任务奖励必须为正数积分")``
— before the replay read, before any wallet work. The mutation
campaign's M10 (``<= 0`` -> ``< 0``, ledger_service) survived every
suite precisely because no test drove a ZERO amount through THIS path:
``post_entry``'s own zero check only fires after the guard, so the
unit pins the guard itself, with a sentinel session that fails the
test on any attribute access — proof the rejection happens before the
first database statement, which is exactly where the mutant would fall
through to the replay read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.points.ledger_service import (
    InvalidLedgerEntryError,
    LedgerService,
)

_LOCK_TIME = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class _UntouchedSession:
    """A sentinel session: ANY attribute access fails the test, so the
    amount guard is proven to reject before the first database use."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(
            "grant_assignment_reward touched the session "
            f"({name!r}) before rejecting the amount"
        )


@pytest.mark.parametrize("amount", [0, -1])
async def test_task_reward_rejects_non_positive_amount_before_any_database_work(
    amount: int,
) -> None:
    """Zero AND negative amounts get the typed rejection at the entry
    point — the M10 boundary: the zero side is what the mutation would
    have let through, and the sentinel session proves no statement runs
    first."""
    service = LedgerService()

    with pytest.raises(InvalidLedgerEntryError) as exc_info:
        await service.grant_assignment_reward(
            cast("AsyncSession", _UntouchedSession()),
            claim_id=uuid4(),
            user_id=uuid4(),
            amount=amount,
            ranking_effective_at=_LOCK_TIME,
        )

    assert str(exc_info.value) == "任务奖励必须为正数积分"
    assert exc_info.value.details is not None
    assert exc_info.value.details["field"] == "amount"
