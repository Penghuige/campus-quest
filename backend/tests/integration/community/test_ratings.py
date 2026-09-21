# backend/tests/integration/community/test_ratings.py
"""Task ratings against real PostgreSQL (spec §20, §31.7; plan 06 task 7).

Order follows the task's risk: the eligibility gate (the surface this
task adds), then the upsert semantics, then the aggregate read, then
concurrency, then the writer gates:

- **Eligibility (spec §20: 只有至少完成过该 Task 一个 Claim 的用户可评分).**
  A COMPLETED AssignmentClaim on the task unlocks rating; CLAIMED-only
  and ABANDONED-only history does not, and a COMPLETED claim on a
  DIFFERENT task unlocks nothing here (the predicate is (user, task)
  scoped). Unknown task ids answer the shared NOT_FOUND.
- **Upsert (spec §20: 评分允许修改，不允许创建多条).** Rate 3 then 5:
  exactly one row, holding 5 — an UPDATE in place, never a second row —
  and the summary moves with it. Two raters are two rows and one
  aggregate.
- **Bounds (rating 1-5).** 1 and 5 are accepted; 0, 6, a float, and a
  bool are the typed VALIDATION_ERROR raised BEFORE any database touch
  (the whitelist-first precedent) — the service-level pin of the
  ck_task_ratings_rating CHECK.
- **Aggregate only (spec §20: 前台只展示聚合分和数量).** The summary is
  average + count and None when nobody rated; no shape in this module
  carries who rated what. The community adapter surfaces the same
  aggregate through the tasks module's RatingSummaryPort.
- **Concurrency (§31.7 at-most-one-row per (task, user)).** Two
  first-ratings released on one barrier both find the row absent and
  both INSERT: UNIQUE(task_id, user_id) arbitrates, the loser's one
  retry replays the WRITE intent onto the winner's committed row, and
  exactly one row survives with both callers succeeding — the
  ratings-specific replay is the same upsert, because unlike reactions
  there is no toggle intent to preserve.
- **Gates.** Rating is a student-surface community write: Student role
  + ACTIVE status on the users row (the shared community writer gate),
  so teachers and admins are the shared typed PERMISSION_DENIED.

Harness notes (the T4/T5 precedent): the race test runs through
``RatingService.rate_task`` with one *independent* session per call,
released simultaneously by an ``asyncio.Event`` barrier after each
connection is warmed with ``SELECT 1``; its committed rows are removed
by explicit committed DELETEs in ``finally`` (task_ratings ->
assignment_claims -> assignments -> tasks -> users, the FK order), and
usernames embed a per-run token. Everything else uses the ordinary
rollback harness.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.community.adapters import CommunityRatingSummaryAdapter
from app.modules.community.gates import (
    CommenterAccountNotActiveError,
    CommenterNotStudentError,
)
from app.modules.community.models import TaskRating
from app.modules.community.rating_service import (
    InvalidRatingError,
    RatingService,
    TaskCompletionRequiredError,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.tasks.enums import (
    AssignmentAvailability,
    ClaimStatus,
    DeadlineMode,
    RewardLockStatus,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.models import Assignment, AssignmentClaim, Task
from app.modules.tasks.query_service import RatingSummary
from app.modules.tasks.service import TaskNotFoundError

# Direct-insert password stub (argon2 hash of an unguessable test secret);
# the registration service is deliberately not exercised here.
_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)

# What a claim's status says about the assignment pool it sat in (spec
# §8.2): occupied while active, back to AVAILABLE after ABANDON,
# COMPLETED when the work was accepted.
_AVAILABILITY_BY_CLAIM = {
    ClaimStatus.CLAIMED: AssignmentAvailability.OCCUPIED,
    ClaimStatus.COMPLETED: AssignmentAvailability.COMPLETED,
    ClaimStatus.ABANDONED: AssignmentAvailability.AVAILABLE,
}


# --- seeding helpers -------------------------------------------------------------


def _user(
    *,
    username: str,
    role: Role = Role.STUDENT,
    status: UserStatus = UserStatus.ACTIVE,
    nickname: str = "测试同学",
) -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname=nickname,
        phone_e164=None,
        role=role,
        status=status,
    )


def _task(owner: User, **overrides: Any) -> Task:
    fields: dict[str, Any] = {
        "owner_teacher_id": owner.id,
        "title": "小红书考研经验帖数据采集",
        "description": "采集指定关键词下的笔记正文与互动数据。",
        "task_type": TaskType.DATA_CRAWL,
        "rarity": TaskRarity.NORMAL,
        "base_reward_points": 100,
        "status": TaskStatus.PUBLISHED,
        "deadline_mode": DeadlineMode.RELATIVE,
        "duration_minutes": 4320,
        "allowed_file_types": ["CSV"],
        "max_file_size_bytes": 200 * 1024 * 1024,
        "notification_channels": ["SMS"],
    }
    fields.update(overrides)
    return Task(**fields)


def _assignment(
    task: Task, keyword: str, availability: AssignmentAvailability
) -> Assignment:
    return Assignment(
        task_id=task.id,
        platform="xiaohongshu",
        keyword=keyword,
        availability_status=availability,
    )


def _claim(assignment: Assignment, user: User, status: ClaimStatus) -> AssignmentClaim:
    deadline = datetime.now(UTC) + timedelta(days=3)
    return AssignmentClaim(
        assignment_id=assignment.id,
        task_id=assignment.task_id,
        user_id=user.id,
        status=status,
        deadline_at=deadline,
        grace_deadline_at=deadline + timedelta(minutes=1440),
        reward_policy_snapshot={"version": 1},
        base_reward_points_snapshot=100,
        submission_schema_version=1,
        reward_lock_status=RewardLockStatus.NONE,
    )


async def _persist(session: AsyncSession, *objects: Any) -> None:
    """Add and flush; parents must be flushed before children reference
    their server-generated ids at construction time."""
    session.add_all(objects)
    await session.flush()


async def _claim_history(
    db: AsyncSession,
    student: User,
    task: Task,
    status: ClaimStatus,
    *,
    keyword: str = "考研英语",
) -> None:
    """Seed one claim of ``status`` for ``student`` on ``task`` with the
    matching assignment availability."""
    assignment = _assignment(task, keyword, _AVAILABILITY_BY_CLAIM[status])
    await _persist(db, assignment)
    await _persist(db, _claim(assignment, student, status))


async def _completed_fixture(
    db: AsyncSession, *, username: str = "20250010001", nickname: str = "完成人"
) -> tuple[Task, User]:
    """A teacher-owned PUBLISHED task plus one student holding a COMPLETED
    claim on it — the minimal eligible rater."""
    teacher = _user(username="teacher0001", role=Role.TEACHER)
    student = _user(username=username, nickname=nickname)
    await _persist(db, teacher, student)
    task = _task(teacher)
    await _persist(db, task)
    await _claim_history(db, student, task, ClaimStatus.COMPLETED)
    return task, student


async def _rows(session: AsyncSession, task_id: UUID) -> list[TaskRating]:
    return list(
        await session.scalars(
            select(TaskRating)
            .where(TaskRating.task_id == task_id)
            .order_by(TaskRating.created_at, TaskRating.id)
        )
    )


# --- eligibility (spec §20: only completers may rate) ------------------------------


@pytest.mark.integration
async def test_completed_claim_unlocks_rating(db_session: AsyncSession) -> None:
    """至少一个 COMPLETED Claim -> the rating lands: exactly one row with
    the caller's (task, user, rating)."""
    task, student = await _completed_fixture(db_session)

    row = await RatingService().rate_task(db_session, student.id, task.id, 4)

    assert row.task_id == task.id
    assert row.user_id == student.id
    assert row.rating == 4
    stored = await _rows(db_session, task.id)
    assert len(stored) == 1
    assert stored[0].rating == 4


@pytest.mark.integration
@pytest.mark.parametrize("status", [ClaimStatus.CLAIMED, ClaimStatus.ABANDONED])
async def test_non_completed_history_cannot_rate(
    db_session: AsyncSession, status: ClaimStatus
) -> None:
    """A claim that never reached COMPLETED — still active (CLAIMED) or
    terminal-without-credit (ABANDONED) — does not unlock rating: the
    typed PERMISSION_DENIED, and no row is ever written."""
    teacher = _user(username="teacher0001", role=Role.TEACHER)
    student = _user(username="20250010001", nickname="未完成人")
    await _persist(db_session, teacher, student)
    task = _task(teacher)
    await _persist(db_session, task)
    await _claim_history(db_session, student, task, status)

    with pytest.raises(TaskCompletionRequiredError) as raised:
        await RatingService().rate_task(db_session, student.id, task.id, 4)
    assert raised.value.code == ErrorCode.PERMISSION_DENIED
    assert raised.value.status_code == 403
    assert raised.value.details == {"user_id": str(student.id), "task_id": str(task.id)}
    assert await _rows(db_session, task.id) == []


@pytest.mark.integration
async def test_completion_on_another_task_does_not_unlock(
    db_session: AsyncSession,
) -> None:
    """The predicate is (user, task) scoped: a COMPLETED claim on a
    DIFFERENT task unlocks nothing here."""
    teacher = _user(username="teacher0001", role=Role.TEACHER)
    student = _user(username="20250010001", nickname="别处完成人")
    await _persist(db_session, teacher, student)
    task = _task(teacher)
    other_task = _task(teacher, title="另一个任务")
    await _persist(db_session, task, other_task)
    await _claim_history(db_session, student, other_task, ClaimStatus.COMPLETED)

    with pytest.raises(TaskCompletionRequiredError):
        await RatingService().rate_task(db_session, student.id, task.id, 5)
    assert await _rows(db_session, task.id) == []
    assert await _rows(db_session, other_task.id) == []


@pytest.mark.integration
async def test_rating_rejects_unknown_task(db_session: AsyncSession) -> None:
    """A garbage task id is the shared NOT_FOUND (existence before
    standing), even for an otherwise-eligible student."""
    _, student = await _completed_fixture(db_session)

    with pytest.raises(TaskNotFoundError) as raised:
        await RatingService().rate_task(db_session, student.id, uuid4(), 4)
    assert raised.value.code == ErrorCode.NOT_FOUND
    assert raised.value.status_code == 404


# --- upsert semantics (spec §20: 评分允许修改，不允许创建多条) -----------------------


@pytest.mark.integration
async def test_re_rate_updates_in_place_and_moves_the_summary(
    db_session: AsyncSession,
) -> None:
    """Rate 3 then 5: DB retains ONE row holding 5 (never a second row;
    the §31.7 UNIQUE anchor), and the summary moves with it."""
    task, student = await _completed_fixture(db_session)
    service = RatingService()

    await service.rate_task(db_session, student.id, task.id, 3)
    assert await service.rating_summary(db_session, task.id) == RatingSummary(
        average=3.0, count=1
    )

    updated = await service.rate_task(db_session, student.id, task.id, 5)

    assert updated.rating == 5
    stored = await _rows(db_session, task.id)
    assert len(stored) == 1
    assert stored[0].rating == 5
    assert await service.rating_summary(db_session, task.id) == RatingSummary(
        average=5.0, count=1
    )


@pytest.mark.integration
async def test_two_raters_are_two_rows_and_one_aggregate(
    db_session: AsyncSession,
) -> None:
    """Each rater owns one row; the summary is the single aggregate over
    them (spec §20: 前台只展示聚合分和数量 — average and count is the
    whole public shape, no per-rater identities)."""
    teacher = _user(username="teacher0001", role=Role.TEACHER)
    first = _user(username="20250010001", nickname="评分人甲")
    second = _user(username="20250010002", nickname="评分人乙")
    await _persist(db_session, teacher, first, second)
    task = _task(teacher)
    await _persist(db_session, task)
    await _claim_history(
        db_session, first, task, ClaimStatus.COMPLETED, keyword="考研英语"
    )
    await _claim_history(
        db_session, second, task, ClaimStatus.COMPLETED, keyword="考研政治"
    )
    service = RatingService()

    await service.rate_task(db_session, first.id, task.id, 3)
    await service.rate_task(db_session, second.id, task.id, 4)

    stored = await _rows(db_session, task.id)
    assert sorted(row.rating for row in stored) == [3, 4]
    assert await service.rating_summary(db_session, task.id) == RatingSummary(
        average=3.5, count=2
    )


# --- bounds (rating 1-5) -------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("rating", [1, 5])
async def test_boundary_ratings_accepted(db_session: AsyncSession, rating: int) -> None:
    """The closed interval's endpoints are ratable — the CHECK is
    inclusive on both ends."""
    task, student = await _completed_fixture(db_session)

    row = await RatingService().rate_task(db_session, student.id, task.id, rating)

    assert row.rating == rating
    assert len(await _rows(db_session, task.id)) == 1


@pytest.mark.integration
@pytest.mark.parametrize("rating", [0, 6, 4.5, True])
async def test_invalid_rating_rejected_before_any_database_touch(
    db_session: AsyncSession, rating: Any
) -> None:
    """Below 1, above 5, a float (the column is INTEGER — asyncpg would
    500 without the guard), and a bool (an int subclass Python-wise) are
    the typed VALIDATION_ERROR, and no row is ever created."""
    task, student = await _completed_fixture(db_session)

    with pytest.raises(InvalidRatingError) as raised:
        await RatingService().rate_task(db_session, student.id, task.id, rating)
    assert raised.value.code == ErrorCode.VALIDATION_ERROR
    assert raised.value.status_code == 400
    assert raised.value.details == {"rating": rating}
    assert await _rows(db_session, task.id) == []


@pytest.mark.integration
async def test_invalid_rating_checked_before_the_gates(
    db_session: AsyncSession,
) -> None:
    """The bounds refusal wins even when the user and task are nonsense:
    the check precedes every gate read (nothing about user/task
    existence is revealed by an invalid rating)."""
    with pytest.raises(InvalidRatingError):
        await RatingService().rate_task(db_session, uuid4(), uuid4(), 0)


# --- writer gates (ratings are a student surface) ------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("role", [Role.TEACHER, Role.ADMIN])
async def test_rating_is_a_student_surface(
    db_session: AsyncSession, role: Role
) -> None:
    """Staff do not rate tasks — the shared community writer gate's
    PERMISSION_DENIED, and no row is written."""
    task, student = await _completed_fixture(db_session)
    staff = _user(username=f"staff-{role.value.lower()}", role=role)
    await _persist(db_session, staff)

    with pytest.raises(CommenterNotStudentError) as raised:
        await RatingService().rate_task(db_session, staff.id, task.id, 4)
    assert raised.value.code == ErrorCode.PERMISSION_DENIED
    assert raised.value.status_code == 403
    assert await _rows(db_session, task.id) == []


@pytest.mark.integration
async def test_rating_requires_an_active_account(db_session: AsyncSession) -> None:
    """A suspended student is the shared ACCOUNT_NOT_ACTIVE refusal —
    completion history does not outlive the account-state gate."""
    teacher = _user(username="teacher0001", role=Role.TEACHER)
    suspended = _user(username="20250010001", status=UserStatus.SUSPENDED)
    await _persist(db_session, teacher, suspended)
    task = _task(teacher)
    await _persist(db_session, task)
    await _claim_history(db_session, suspended, task, ClaimStatus.COMPLETED)

    with pytest.raises(CommenterAccountNotActiveError) as raised:
        await RatingService().rate_task(db_session, suspended.id, task.id, 4)
    assert raised.value.code == ErrorCode.ACCOUNT_NOT_ACTIVE
    assert raised.value.status_code == 403
    assert await _rows(db_session, task.id) == []


# --- the aggregate read and the cross-module adapter ---------------------------------


@pytest.mark.integration
async def test_summary_is_none_with_zero_ratings(db_session: AsyncSession) -> None:
    """Nobody rated -> None, not a zero average: the port contract that
    lets the public surfaces render "not rated yet"."""
    task, _ = await _completed_fixture(db_session)

    assert await RatingService().rating_summary(db_session, task.id) is None
    adapter = CommunityRatingSummaryAdapter(db_session)
    assert await adapter.summary(task.id) is None


@pytest.mark.integration
async def test_summary_is_scoped_to_one_task(db_session: AsyncSession) -> None:
    """The aggregate reads only the one task's rows — a rated neighbor
    task leaks nothing in."""
    teacher = _user(username="teacher0001", role=Role.TEACHER)
    student = _user(username="20250010001", nickname="评分人")
    await _persist(db_session, teacher, student)
    task = _task(teacher)
    neighbor = _task(teacher, title="隔壁任务")
    await _persist(db_session, task, neighbor)
    await _claim_history(db_session, student, task, ClaimStatus.COMPLETED)
    await _claim_history(db_session, student, neighbor, ClaimStatus.COMPLETED)

    await RatingService().rate_task(db_session, student.id, task.id, 5)

    assert await RatingService().rating_summary(db_session, task.id) == RatingSummary(
        average=5.0, count=1
    )
    assert await RatingService().rating_summary(db_session, neighbor.id) is None


@pytest.mark.integration
async def test_adapter_surfaces_the_aggregate_through_the_port(
    db_session: AsyncSession,
) -> None:
    """The community adapter answers the tasks module's RatingSummaryPort
    shape (imported, never redefined): None until rated, then the live
    aggregate — the same numbers the service's own read returns."""
    task, student = await _completed_fixture(db_session)
    adapter = CommunityRatingSummaryAdapter(db_session)

    assert await adapter.summary(task.id) is None
    await RatingService().rate_task(db_session, student.id, task.id, 4)
    assert await adapter.summary(task.id) == RatingSummary(average=4.0, count=1)


# --- concurrency (§31.7 at-most-one-row per (task, user)) -----------------------------


@dataclasses.dataclass(slots=True)
class RateOutcome:
    """One rate attempt's terminal state: the row, a business error, or —
    never, if the service is correct — an unexpected exception."""

    row: TaskRating | None = None
    error: BusinessError | None = None
    unexpected: BaseException | None = None


async def _rate_one(
    service: RatingService,
    factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    task_id: UUID,
    rating: int,
    start: asyncio.Event,
) -> RateOutcome:
    async with factory() as session:
        # Warm the pooled connection BEFORE the barrier (asyncpg setup is
        # ~10 ms of TCP + auth; without the warm-up the barrier releases
        # into sequential-looking runs that hide the interleaving under
        # test — the claim-concurrency lesson).
        await session.execute(text("SELECT 1"))
        await start.wait()  # park every transaction on one barrier
        try:
            row = await service.rate_task(session, user_id, task_id, rating)
        except BusinessError as exc:
            return RateOutcome(error=exc)
        except Exception as exc:  # the "no 500" failure mode
            return RateOutcome(unexpected=exc)
        return RateOutcome(row=row)


@pytest.mark.integration
async def test_concurrent_first_ratings_end_as_one_row(db_engine: AsyncEngine) -> None:
    """Two first-ratings (3 and 5) released on one barrier both find the
    row absent and both INSERT: UNIQUE(task_id, user_id) (§31.7) makes
    exactly one insert win; the loser's flush surfaces IntegrityError,
    rolls back, and its ONE retry replays the WRITE intent — which for
    ratings is the same upsert, so it UPDATEs the winner's row to its
    own value. Every legal interleaving ends with EXACTLY ONE row, both
    callers succeed, and no exception escapes as a 500."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    service = RatingService()
    run = uuid4().hex[:8]

    user_ids: list[UUID] = []
    task_ids: list[UUID] = []
    claim_ids: list[UUID] = []
    assignment_ids: list[UUID] = []
    try:
        async with factory() as session:
            teacher = _user(username=f"t{run}", role=Role.TEACHER)
            student = _user(username=f"2025{run}001", nickname="并发评分人")
            await _persist(session, teacher, student)
            task = _task(teacher)
            await _persist(session, task)
            assignment = _assignment(task, "考研英语", AssignmentAvailability.COMPLETED)
            await _persist(session, assignment)
            claim = _claim(assignment, student, ClaimStatus.COMPLETED)
            await _persist(session, claim)
            await session.commit()
            task_ids.append(task.id)
            assignment_ids.append(assignment.id)
            claim_ids.append(claim.id)
            user_ids.extend([teacher.id, student.id])

        start = asyncio.Event()
        calls = [
            asyncio.create_task(
                _rate_one(service, factory, student.id, task_ids[0], rating, start)
            )
            for rating in (3, 5)
        ]
        await asyncio.sleep(0.05)  # let every coroutine reach the barrier
        start.set()
        outcomes = list(await asyncio.wait_for(asyncio.gather(*calls), timeout=15))

        assert not [o for o in outcomes if o.unexpected is not None], [
            repr(o.unexpected) for o in outcomes if o.unexpected is not None
        ]
        assert not [o for o in outcomes if o.error is not None], [
            repr(o.error) for o in outcomes if o.error is not None
        ]

        async with factory() as session:
            rows = await _rows(session, task_ids[0])
            assert len(rows) == 1  # never two rows for one (task, user)
            assert rows[0].user_id == student.id
            winner = rows[0].rating
            assert winner in (3, 5)
            summary = await service.rating_summary(session, task_ids[0])
            assert summary == RatingSummary(average=float(winner), count=1)
    finally:
        async with factory() as session:
            await session.execute(
                delete(TaskRating).where(TaskRating.task_id.in_(task_ids))
            )
            await session.execute(
                delete(AssignmentClaim).where(AssignmentClaim.id.in_(claim_ids))
            )
            await session.execute(
                delete(Assignment).where(Assignment.id.in_(assignment_ids))
            )
            await session.execute(delete(Task).where(Task.id.in_(task_ids)))
            await session.execute(delete(User).where(User.id.in_(user_ids)))
            await session.commit()
