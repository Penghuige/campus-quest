# backend/tests/unit/community/test_rendering_fixpoint.py
"""Deterministic pins for the order-sensitive coverage face (flake hunt
2026-10-17): the CI-vs-local diff landed on exactly three lines — the
rating retry orchestration's IntegrityError legs (rating_service 174,
176) and the rendering fixpoint's tombstone-propagation branch
(comment_service 803). The EXISTING integration tests reach them only
through real races (two transactions on one barrier) or incidental
data shapes, so CI's timing sometimes never exercises them — the PR #36
prescription: pin the full shape deterministically, source untouched.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.modules.community.comment_service import CommentService
from app.modules.community.models import Comment
from app.modules.community.rating_service import RatingService

_UQ = "uq_task_ratings_task_id_user_id"


def _integrity_error(constraint: str) -> IntegrityError:
    return IntegrityError(
        statement="INSERT INTO task_ratings ...",
        params={},
        orig=RuntimeError(
            f'duplicate key value violates unique constraint "{constraint}"'
        ),
    )


class _RollbackRecorder:
    """The retry leg's only db need: a rollback we can observe."""

    def __init__(self) -> None:
        self.rollbacks = 0

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_first_conflict_rolls_back_and_retries_the_write() -> None:
    """attempt 0 hits the target constraint: rollback, then the replay
    succeeds (the 'both-create' race's deterministic twin)."""
    service = RatingService()
    db = _RollbackRecorder()
    calls: list[int] = []

    async def _flaky_first(db_arg, user_id, task_id, rating):
        calls.append(rating)
        if len(calls) == 1:
            raise _integrity_error(_UQ)
        return "replayed-write-landed"

    service._attempt = _flaky_first  # type: ignore[method-assign]
    result = await service.rate_task(db, uuid4(), uuid4(), 4)

    # The ORCHESTRATION is what this pin owns: exactly one rollback on
    # the first conflict, and the write replayed a second time.
    assert result == "replayed-write-landed"
    assert db.rollbacks == 1
    assert calls == [4, 4]


@pytest.mark.asyncio
async def test_second_conflict_is_terminal() -> None:
    """attempt 1 hitting the same constraint again raises straight out —
    no third try, no rollback loop."""
    service = RatingService()
    db = _RollbackRecorder()

    async def _always_conflicts(db_arg, user_id, task_id, rating):
        raise _integrity_error(_UQ)

    service._attempt = _always_conflicts  # type: ignore[method-assign]
    with pytest.raises(IntegrityError):
        await service.rate_task(db, uuid4(), uuid4(), 4)
    assert db.rollbacks == 1  # only the first conflict rolled back


@pytest.mark.asyncio
async def test_foreign_constraint_never_retries() -> None:
    """A DIFFERENT unique constraint is not the both-create race: it
    raises on attempt 0 without a single rollback."""
    service = RatingService()
    db = _RollbackRecorder()

    async def _foreign(db_arg, user_id, task_id, rating):
        raise _integrity_error("uq_somewhere_else")

    service._attempt = _foreign  # type: ignore[method-assign]
    with pytest.raises(IntegrityError):
        await service.rate_task(db, uuid4(), uuid4(), 4)
    assert db.rollbacks == 0


def _comment(
    *, deleted: bool, parent: UUID | None = None, comment_id: UUID | None = None
) -> Comment:
    from datetime import UTC, datetime

    return Comment(
        id=comment_id or uuid4(),
        task_id=uuid4(),
        user_id=uuid4(),
        parent_id=parent,
        content="pin",
        deleted_at=datetime.now(UTC) if deleted else None,
    )


def test_tombstone_renders_when_a_child_renders() -> None:
    """The fixpoint branch the CI timing skips: a soft-deleted parent
    whose child renders must itself render (as a tombstone) — and the
    propagation keeps climbing through a deleted GRANDparent."""
    live_child = _comment(deleted=False)
    deleted_parent = _comment(deleted=True, parent=uuid4(), comment_id=uuid4())
    deleted_grandparent = _comment(
        deleted=True, parent=uuid4(), comment_id=deleted_parent.parent_id
    )
    # child's parent is the deleted parent; that parent hangs off the
    # deleted grandparent.
    live_child.parent_id = deleted_parent.id
    rows = [
        (live_child, "nick", False),
        (deleted_parent, "nick", False),
        (deleted_grandparent, "nick", False),
    ]

    rendered = CommentService._rendered_comment_ids(rows)

    assert live_child.id in rendered
    assert deleted_parent.id in rendered  # the tombstone branch
    assert deleted_grandparent.id in rendered  # the loop's second pass


def test_fully_deleted_thread_renders_nothing() -> None:
    """With no live descendant, deleted comments never render — the
    fixpoint's False fixpoint (the branch's negative shape)."""
    deleted = _comment(deleted=True)
    rendered = CommentService._rendered_comment_ids([(deleted, "n", False)])
    assert rendered == set()
