# backend/app/modules/community/rating_service.py
"""Task ratings (spec §20, §31.7; plan 06 task 7).

Design decisions:

- **Bounds checked FIRST (rating 1-5).** A pure integer-interval check
  (1 and 5 inclusive) raised before any database touch — the reaction
  whitelist-first precedent, so an invalid rating reveals nothing about
  user or task existence. The database's own ``ck_task_ratings_rating``
  CHECK stays the last-resort anchor; the service pin is what the spec
  §38.7 acceptance list exercises. The type guard is load-bearing, not
  paranoia: the column is INTEGER, so a float or a bool (an int subclass
  Python-wise) that slipped past the range check would die in asyncpg's
  parameter binding as a 500, never a typed envelope.
- **Writer gate: the rating gate (gates.py).** Rating stays
  Student-only (spec §20: eligibility is the completed-claim
  predicate, and only Students hold claims, spec §4.1): Student role
  and ACTIVE status judged on the users ROW through
  ``require_student_writer`` — deliberately NOT the participant gate
  comments/votes/reactions/reports moved to (PR #2 hardening: Teacher
  joined the ordinary community surface; rating eligibility did not
  change). Staff reach tasks through their own surfaces; a completer's
  teacher/admin counterpart does not exist.
- **Existence before eligibility.** An unknown task id is the shared
  ``TaskNotFoundError`` (the moderate-delete ordering: existence answers
  before standing). Deliberately NOT gated on PUBLISHED: the comment
  visibility rule governs reads anchored on comments, while rating
  eligibility IS completion history — a completer may rate a task that
  has since CLOSED or been ARCHIVED, and no spec clause retracts that
  right when the task leaves the public surface.
- **Eligibility (spec §20: 只有至少完成过该 Task 一个 Claim 的用户可评分).**
  At least one AssignmentClaim with status COMPLETED for the (user,
  task) pair — a read-only query over the tasks module's rows, the
  sanctioned cross-module seam (the gates/report precedent of importing
  the tasks models for reads; no write, no invariant borrowed). The
  predicate is judged at write time only: a rating survives every later
  claim-history change, and nothing cascades from claims to ratings —
  the §31.7 structural notes in models.py.
- **Upsert (spec §20: 评分允许修改，不允许创建多条).** The per-(task,
  user) row IS the rating: absent -> INSERT; present -> UPDATE in place
  (``updated_at`` moves via the column's onupdate). The existing row is
  locked ``FOR UPDATE`` (one serialization point per pair) before the
  branch is taken.
- **Both-create retry (§31.7).** ``FOR UPDATE`` on an absent row locks
  nothing, so two concurrent first-ratings both INSERT and the
  UNIQUE(task_id, user_id) constraint arbitrates: the loser's flush
  surfaces ``IntegrityError``, the transaction rolls back, and exactly
  ONE retry runs. The retry REPLAYS THE WRITE intent — which for
  ratings is simply the same upsert (unlike reactions there is no
  toggle judgment whose meaning would invert on replay), so it lands as
  an UPDATE on the winner's committed row: last committed write wins,
  which is exactly "one row holding the latest rating". A second
  violation, or an IntegrityError naming any other constraint, re-raises
  untouched.
- **Aggregate only (spec §20: 前台只展示聚合分和数量).**
  ``rating_summary`` returns ``RatingSummary(average, count)`` — the
  port DTO the tasks module owns — and nothing else: no shape in this
  module carries who rated what, because no such shape exists. Zero
  rows report None (the port's None-safety contract: "not rated yet",
  never a misleading 0.0 average). PostgreSQL's ``avg`` over INTEGER
  comes back NUMERIC; ``float()`` at the boundary is the one
  conversion.
- **No clock and no event seam.** ``created_at``/``updated_at`` are
  database defaults, and the spec wires no audit stream to rating
  writes; task 9's router composes the echo surface.

Transaction shape per backend-engineering §5: the gate and eligibility
reads, the locked write, and exactly one commit per call;
``rating_summary`` is read-only.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.community.gates import require_student_writer
from app.modules.community.models import TaskRating
from app.modules.tasks.enums import ClaimStatus
from app.modules.tasks.models import AssignmentClaim, Task
from app.modules.tasks.query_service import RatingSummary
from app.modules.tasks.service import TaskNotFoundError

__all__ = [
    "InvalidRatingError",
    "RatingService",
    "TaskCompletionRequiredError",
]

# The (task_id, user_id) anchor (spec §31.7) — the only IntegrityError
# this service expects and retries.
_UQ_TASK_RATINGS = "uq_task_ratings_task_id_user_id"

# Driver-agnostic constraint-name extraction, kept identical to the vote
# and reaction services' private helpers (asyncpg exposes
# .constraint_name, other drivers only the message).
_CONSTRAINT_IN_MESSAGE = re.compile(r'constraint "(?P<name>[^"]+)"')

_INVALID_RATING_MESSAGE = "评分必须是 1 到 5 之间的整数"
_COMPLETION_REQUIRED_MESSAGE = "只有完成过该任务至少一个领取的用户可评分"


class InvalidRatingError(BusinessError):
    """``rating`` is outside the closed 1-5 integer interval — including
    floats and bools, which the INTEGER column could never store."""

    def __init__(self, rating: Any) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _INVALID_RATING_MESSAGE,
            status_code=400,
            details={"rating": rating},
        )


class TaskCompletionRequiredError(BusinessError):
    """The rater holds no COMPLETED AssignmentClaim on this task (spec
    §20 只有至少完成过该 Task 一个 Claim 的用户可评分) — capability, so
    the PERMISSION_DENIED family, not a validation failure."""

    def __init__(self, user_id: UUID, task_id: UUID) -> None:
        super().__init__(
            ErrorCode.PERMISSION_DENIED,
            _COMPLETION_REQUIRED_MESSAGE,
            status_code=403,
            details={"user_id": str(user_id), "task_id": str(task_id)},
        )


def _constraint_name(exc: IntegrityError) -> str | None:
    """The violated constraint's name, from the driver or the message."""
    name = getattr(exc.orig, "constraint_name", None)
    if isinstance(name, str) and name:
        return name
    match = _CONSTRAINT_IN_MESSAGE.search(str(exc.orig))
    return match.group("name") if match is not None else None


def _is_valid_rating(rating: Any) -> bool:
    """The closed 1-5 interval over real ints: bool is an int subclass
    Python-wise but never a rating, and a float binding to INTEGER is a
    driver error, not an envelope."""
    return isinstance(rating, int) and not isinstance(rating, bool) and 1 <= rating <= 5


class RatingService:
    """The task rating surface (spec §20): ``rate_task`` is the whole
    write, ``rating_summary`` the aggregate read the port adapter
    delegates to."""

    async def rate_task(
        self, db: AsyncSession, user_id: UUID, task_id: UUID, rating: int
    ) -> TaskRating:
        """Rate (or re-rate) ``task_id`` as ``user_id``: exactly one row
        per (task, user) holding the latest rating. See the module
        docstring for the bounds-first, gate, eligibility, and retry
        rulings."""
        if not _is_valid_rating(rating):
            raise InvalidRatingError(rating)
        # One retry covers both-create-from-absent, and it REPLAYS the
        # write intent — for ratings the same upsert, which lands as an
        # UPDATE on the winner's committed row (last write wins).
        for attempt in (0, 1):
            try:
                return await self._attempt(db, user_id, task_id, rating)
            except IntegrityError as exc:
                if attempt == 1 or _constraint_name(exc) != _UQ_TASK_RATINGS:
                    raise
                await db.rollback()
        raise AssertionError("unreachable: the loop returns or raises")

    async def rating_summary(
        self, db: AsyncSession, task_id: UUID
    ) -> RatingSummary | None:
        """The public aggregate of one task's ratings (spec §20: 聚合分
        和数量): None when nobody rated, else average + count — never any
        per-rater identity, which no shape here carries."""
        average, count = (
            await db.execute(
                select(func.avg(TaskRating.rating), func.count()).where(
                    TaskRating.task_id == task_id
                )
            )
        ).one()
        if count == 0:
            return None
        return RatingSummary(average=float(average), count=int(count))

    # -- internals ----------------------------------------------------------------

    async def _attempt(
        self,
        db: AsyncSession,
        user_id: UUID,
        task_id: UUID,
        rating: int,
    ) -> TaskRating:
        """One full locked attempt: bounds -> gates -> existence ->
        eligibility -> FOR UPDATE -> upsert -> commit."""
        await require_student_writer(db, user_id)
        if await db.scalar(select(Task.id).where(Task.id == task_id)) is None:
            raise TaskNotFoundError(task_id)
        completed = await db.scalar(
            select(AssignmentClaim.id)
            .where(
                AssignmentClaim.task_id == task_id,
                AssignmentClaim.user_id == user_id,
                AssignmentClaim.status == ClaimStatus.COMPLETED.value,
            )
            .limit(1)
        )
        if completed is None:
            raise TaskCompletionRequiredError(user_id, task_id)

        row = await db.scalar(
            select(TaskRating)
            .where(TaskRating.task_id == task_id, TaskRating.user_id == user_id)
            .with_for_update()
        )
        if row is None:
            # absent -> INSERT; the UNIQUE anchor arbitrates the race.
            row = TaskRating(task_id=task_id, user_id=user_id, rating=rating)
            db.add(row)
        else:
            # present -> UPDATE in place (评分允许修改，不允许创建多条).
            row.rating = rating
        await db.flush()
        await db.commit()
        await db.refresh(row)  # load the now()/onupdate server defaults
        return row
