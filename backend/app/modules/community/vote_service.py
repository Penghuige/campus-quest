# backend/app/modules/community/vote_service.py
"""Like/dislike vote toggle (spec §22, §31.8; plan 06 task 4).

Design decisions:

- **"none" is the absence of a row.** ``value=0`` DELETEs the row: spec
  §22 allows none->like / like->none / like->dislike / dislike->like over
  a UNIQUE(comment_id, user_id) anchor, and "none" means no row — no
  ``value=0`` placeholder is ever stored, so the column's CHECK (1, -1)
  domain and the table stay exactly the spec's shape.
- **Writer gate: the T2 rule with the shared typed errors.** Voting is a
  community write: Student role and ACTIVE status, judged on the users
  ROW (role-first, then status), refusing with comment_service's
  ``CommenterNotStudentError`` / ``CommenterAccountNotActiveError`` /
  ``CommenterNotFoundError`` so the whole community write surface answers
  with one set of codes. No per-user write invariant exists to protect
  (the vote row itself is the invariant), so the read carries no lock —
  the T2 create-gate precedent.
- **Comment visibility: the public-surface rule.** The comment must exist
  (``CommentNotFoundError``) and its task must be PUBLISHED — anything
  else is the shared ``TaskNotFoundError``, the same
  invisibility-reads-as-NOT_FOUND ruling as create/list. A tombstone is
  not a votable anchor: a comment with ``deleted_at`` set is refused with
  ``CommentDeletedError``, and because the Admin hard hide writes the
  soft-delete trio too, that ONE guard covers both removal kinds (the T3
  reply-guard precedent). The comment and task rows are read WITHOUT
  locks: publish-immediately semantics — a vote that commits while the
  comment is concurrently soft-deleted simply survives as a row on a
  tombstone, the same ruling as a comment committing while its task
  pauses; removing it is moderation's business, not the voter's.
- **Atomic transitions (spec §22: 切换 MUST 原子化).** The existing vote
  row is locked ``FOR UPDATE`` — one serialization point per (comment,
  user) — and the transition applies on the locked row: UPDATE ``value``
  for a flip, a core DELETE for like/dislike->none, an INSERT for
  none->like/dislike. Same-value re-votes and remove-when-none are
  idempotent no-ops.
- **Both-create-from-none.** ``FOR UPDATE`` on an absent row locks
  nothing, so two concurrent none->X inserts race and the
  UNIQUE(comment_id, user_id) constraint is the database-side anchor
  (§31.8): the loser's INSERT flush surfaces ``IntegrityError``, the
  transaction rolls back, and exactly ONE retry of the whole locked path
  runs — the violation only surfaces after the winner COMMITTED, so the
  retry's ``FOR UPDATE`` reliably finds that row and applies the loser's
  transition onto it (final state: one row, the retrier's value). A
  second violation or an IntegrityError naming any other constraint
  re-raises untouched.
- **Counts.** ``likes``/``dislikes`` are one grouped COUNT over the
  comment's rows, read in the SAME transaction after the transition
  flushed, so the returned totals include this transaction's effect.
  Other users' concurrently committing votes may or may not be visible
  depending on statement timing (READ COMMITTED): the result is
  consistent with the rows as of this transaction's own commit, which is
  what the echoing surface needs (the race tests pin the same-user
  serialization this guarantees).

Transaction shape per backend-engineering §5: the gate reads, the locked
transition, the counts, and exactly one commit per call.
"""

from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import String, Uuid, column, delete, func, select, table
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.community.comment_service import (
    CommentDeletedError,
    CommenterAccountNotActiveError,
    CommenterNotFoundError,
    CommenterNotStudentError,
    CommentNotFoundError,
)
from app.modules.community.models import Comment, CommentVote
from app.modules.community.schemas import VoteResult
from app.modules.identity.enums import Role, UserStatus
from app.modules.tasks.enums import TaskStatus
from app.modules.tasks.models import Task
from app.modules.tasks.service import TaskNotFoundError

__all__ = ["InvalidVoteValueError", "VoteService"]

# The (comment_id, user_id) anchor (spec §31.8) — the only IntegrityError
# this service expects and retries.
_UQ_COMMENT_VOTES = "uq_comment_votes_comment_id_user_id"

# Driver-agnostic constraint-name extraction from the claim service (kept
# local here: a private helper there, a private helper here — asyncpg
# exposes .constraint_name, other drivers only the message).
_CONSTRAINT_IN_MESSAGE = re.compile(r'constraint "(?P<name>[^"]+)"')

# Identity seam (the T2 precedent): a typed Core-level light users table,
# NOT the identity ORM model — role/status for the writer gate.
_USERS = table(
    "users",
    column("id", Uuid),
    column("role", String),
    column("status", String),
)

_INVALID_VALUE_MESSAGE = "投票取值只能是 -1、0 或 1"


class InvalidVoteValueError(BusinessError):
    """``value`` outside the spec §22 domain {-1, 0, 1} (the column
    CHECK's (1, -1) plus the remove sentinel 0)."""

    def __init__(self, value: object) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _INVALID_VALUE_MESSAGE,
            status_code=400,
            details={"value": str(value)},
        )


def _constraint_name(exc: IntegrityError) -> str | None:
    """The violated constraint's name, from the driver or the message."""
    name = getattr(exc.orig, "constraint_name", None)
    if isinstance(name, str) and name:
        return name
    match = _CONSTRAINT_IN_MESSAGE.search(str(exc.orig))
    return match.group("name") if match is not None else None


class VoteService:
    """The like/dislike toggle (spec §22): ``set_vote`` is the whole
    surface. No clock and no event seam — ``created_at`` is a database
    ``now()`` server default and the spec wires no audit stream to plain
    votes (moderation-adjacent flows live in the report service)."""

    async def set_vote(
        self, db: AsyncSession, user_id: UUID, comment_id: UUID, value: int
    ) -> VoteResult:
        """Transition this user's stance on one comment to ``value`` (-1,
        0, or 1; 0 removes the vote) and return the post-transition stance
        plus the comment's like/dislike totals. See the module docstring
        for the gate, locking, retry, and count rulings."""
        if value not in (-1, 0, 1):
            raise InvalidVoteValueError(value)
        # One retry covers both-create-from-none: the loser's IntegrityError
        # surfaces only after the winner committed, so the retry's FOR
        # UPDATE finds the winner's row and applies this caller's
        # transition onto it.
        for attempt in (0, 1):
            try:
                return await self._transition(db, user_id, comment_id, value)
            except IntegrityError as exc:
                if attempt == 1 or _constraint_name(exc) != _UQ_COMMENT_VOTES:
                    raise
                await db.rollback()
        raise AssertionError("unreachable: the loop returns or raises")

    # -- internals ----------------------------------------------------------------

    async def _transition(
        self, db: AsyncSession, user_id: UUID, comment_id: UUID, value: int
    ) -> VoteResult:
        """One full locked attempt: gates -> FOR UPDATE -> apply ->
        counts -> commit. Re-entered exactly once by ``set_vote`` after a
        both-create-from-none UNIQUE violation."""
        await self._require_student_voter(db, user_id)
        await self._require_votable_comment(db, comment_id)

        vote = await db.scalar(
            select(CommentVote)
            .where(
                CommentVote.comment_id == comment_id,
                CommentVote.user_id == user_id,
            )
            .with_for_update()
        )
        if value == 0:
            if vote is not None:
                # like/dislike -> none: none is the ABSENCE of the row.
                await db.execute(delete(CommentVote).where(CommentVote.id == vote.id))
        elif vote is None:
            # none -> like/dislike; the UNIQUE anchor arbitrates the race.
            db.add(CommentVote(comment_id=comment_id, user_id=user_id, value=value))
            await db.flush()
        elif vote.value != value:
            vote.value = value  # flip on the locked row, in place
            await db.flush()

        likes, dislikes = await self._counts(db, comment_id)
        await db.commit()
        return VoteResult(current_value=value, likes=likes, dislikes=dislikes)

    @staticmethod
    async def _counts(db: AsyncSession, comment_id: UUID) -> tuple[int, int]:
        """(likes, dislikes) over the comment's rows — one grouped COUNT,
        the query_service statistics precedent."""
        rows = await db.execute(
            select(CommentVote.value, func.count())
            .where(CommentVote.comment_id == comment_id)
            .group_by(CommentVote.value)
        )
        totals = {value: int(count) for value, count in rows}
        return totals.get(1, 0), totals.get(-1, 0)

    @staticmethod
    async def _require_student_voter(db: AsyncSession, user_id: UUID) -> None:
        """The T2 writer gate on the users row: role-first, then status,
        with the shared community typed errors."""
        voter = (
            await db.execute(
                select(_USERS.c.role, _USERS.c.status).where(_USERS.c.id == user_id)
            )
        ).first()
        if voter is None:
            raise CommenterNotFoundError(user_id)
        role, status = voter
        if role != Role.STUDENT.value:
            raise CommenterNotStudentError(user_id, str(role))
        if status != UserStatus.ACTIVE.value:
            raise CommenterAccountNotActiveError(user_id)

    @staticmethod
    async def _require_votable_comment(db: AsyncSession, comment_id: UUID) -> None:
        """The comment exists, is not a tombstone (soft delete OR Admin
        hard hide — one trio guard, the T3 precedent), and sits on a
        PUBLISHED task. Unlocked reads: publish-immediately semantics."""
        comment = await db.scalar(select(Comment).where(Comment.id == comment_id))
        if comment is None:
            raise CommentNotFoundError(comment_id)
        if comment.deleted_at is not None:
            raise CommentDeletedError(comment_id)
        published = await db.scalar(
            select(Task.id).where(
                Task.id == comment.task_id,
                Task.status == TaskStatus.PUBLISHED.value,
            )
        )
        if published is None:
            raise TaskNotFoundError(comment.task_id)
