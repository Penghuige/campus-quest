# backend/tests/unit/submissions/test_owner_row_missing_denies.py
"""A missing task-owner row must DENY, never widen access (hardening B-F2).

Both reviewer-standing checks used to treat ``owner_id is None`` as the
allow branch. The ``submissions.claim_id -> assignment_claims.task_id ->
tasks.owner_teacher_id`` FK chain makes the None row unreachable in a
consistent database, which is exactly why the tests stub the session:
they pin the DEFENSIVE default (fail closed) so a future schema or query
change that can produce None cannot silently turn "owner unknown" into
"access for everyone".
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.clock import FrozenClock
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor, InMemoryEventCollector
from app.modules.submissions.query_service import (
    SubmissionNotOwnedError,
    SubmissionQueryService,
)
from app.modules.submissions.review_service import (
    ReviewerPermissionDeniedError,
    ReviewService,
)
from tests.fakes.points import FakePointsRewardPort

_T0 = datetime(2026, 10, 5, 8, 0, 0, tzinfo=UTC)


class _ScriptedSession:
    """AsyncSession stand-in: ``scalar`` answers from a scripted sequence
    and ``rollback`` is counted (the review deny path rolls back first)."""

    def __init__(self, scalar_results: list[object]) -> None:
        self._results = list(scalar_results)
        self.rollbacks = 0

    async def scalar(self, *_args: object, **_kwargs: object) -> object:
        return self._results.pop(0)

    async def rollback(self) -> None:
        self.rollbacks += 1


def _teacher_actor() -> Actor:
    return Actor(user_id=uuid4(), role=Role.TEACHER)


async def test_download_standing_denies_when_owner_row_is_missing() -> None:
    # First scalar (the owner lookup) answers None: no task owner row.
    db = _ScriptedSession([None])
    service = SubmissionQueryService()

    with pytest.raises(SubmissionNotOwnedError):
        await service._require_download_standing(
            db,
            SimpleNamespace(task_id=uuid4()),
            _teacher_actor(),
            uuid4(),  # submission_id (read only by the denial error)
        )


async def test_review_permission_denies_when_owner_row_is_missing() -> None:
    db = _ScriptedSession([None])
    service = ReviewService(
        clock=FrozenClock(_T0),
        events=InMemoryEventCollector(),
        points=FakePointsRewardPort(),
    )

    with pytest.raises(ReviewerPermissionDeniedError):
        await service._require_review_permission(
            db, SimpleNamespace(task_id=uuid4()), _teacher_actor()
        )
    # The deny path rolls the session back like the adjacent collaborator
    # denial (the review flow may hold locks by the time it runs).
    assert db.rollbacks == 1
