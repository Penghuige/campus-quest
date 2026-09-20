# backend/tests/integration/submissions/test_reward_lock.py
"""Reward lock + claim transition after machine validation (spec §11.2,
§11.5, §26, §8.1; plan 04 task 8).

Covers the lock semantics of ``RewardLockService.on_validation_passed``:

- the FIRST machine-passed submission locks PROVISIONAL at the tier
  computed from ITS ``submitted_at`` (spec §11.2: 100% at the deadline,
  80% at +2h) — never from worker/review time: every service call here
  runs with the business clock already PAST grace, and the locked tier
  still reflects the in-window submit instant (§11.5);
- ``locked_reward_points`` floors the claim's ``base_reward_points_snapshot``
  and ``reward_locked_at`` equals the submission instant;
- later valid revisions never lower an existing non-invalidated lock
  (PROVISIONAL and CONFIRMED are preserved untouched);
- an INVALIDATED lock lets the next valid submission establish a NEW
  PROVISIONAL at its own fraction, appending history (the invalidate
  ACTION is task 9 — the precondition is direct-seeded here);
- replay is idempotent: the current claim is returned, no duplicate
  history row, no duplicate event;
- a terminal claim (the expiry-won ordering) turns the call into a
  no-op that never resurrects the claim and never locks;
- a VALIDATED submission whose ``submitted_at`` is at/after grace is
  data corruption: a typed error, no transition;
- a submission that is not VALIDATED is a caller sequencing bug: typed
  rejection, nothing written;
- the submit-vs-expire race at the grace boundary (one of the five
  review-focus races; spec §26's 23:59:59.900 vs 24:00:00.000):
  ``on_validation_passed`` and a test-local expiry candidate run
  CONCURRENTLY (barrier, independent sessions) — see the two race
  tests below for the legality contract each side enforces.

Harness notes (same conventions as test_validation_worker.py): explicit
committed sessions from a NullPool engine factory — every asyncio.run
phase runs on a fresh event loop, so pooled connections must never be
reused across them; usernames embed a per-run token; every test removes
its rows with committed DELETEs in FK order in ``finally``.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import InMemoryEventCollector
from app.modules.identity.models import User
from app.modules.submissions.enums import ValidationStatus
from app.modules.submissions.models import (
    RewardLockHistory,
    Submission,
    SubmissionValidation,
)
from app.modules.submissions.reward_lock_service import (
    REWARD_LOCKED,
    RewardLockService,
    RewardWindowInconsistentError,
    SubmissionNotValidatedError,
)
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

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)
# The submission window: deadline at 09:00, grace 24h later. The service
# clock in every non-race test is PAST grace — the locked tier must still
# come from the in-window submitted_at (spec §11.5).
_DEADLINE = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
_GRACE = _DEADLINE + timedelta(hours=24)
_NOW = _GRACE + timedelta(milliseconds=1)
_CLAIMED_AT = _DEADLINE - timedelta(days=3)

_TERMINAL_CLAIM_STATUSES = (
    ClaimStatus.COMPLETED,
    ClaimStatus.ABANDONED,
    ClaimStatus.EXPIRED,
)


@dataclass(slots=True)
class Seed:
    teacher: User
    student: User
    task: Task
    assignment: Assignment
    claim: AssignmentClaim
    submissions: dict[int, Submission]


@dataclass(frozen=True, slots=True)
class SubmissionSpec:
    version: int
    submitted_at: datetime
    validation_status: str = ValidationStatus.VALIDATED.value


@dataclass(frozen=True, slots=True)
class HistorySpec:
    """One direct-seeded RewardLockHistory row (the INVALIDATED
    precondition; the invalidate action itself is task 9)."""

    lock_status_from: str | None
    lock_status_to: str
    version: int
    reward_tier_locked: int | None = None
    locked_reward_points: int | None = None


def _new_factory() -> async_sessionmaker[AsyncSession]:
    """NullPool session factory: fresh connection per checkout, so
    asyncio.run phases on fresh loops never share a pooled connection."""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    run: str,
    *,
    claim_status: ClaimStatus = ClaimStatus.CLAIMED,
    reward_lock_status: RewardLockStatus = RewardLockStatus.NONE,
    reward_tier_locked: int | None = None,
    locked_reward_points: int | None = None,
    reward_locked_at: datetime | None = None,
    latest_version: int | None = None,
    submissions: tuple[SubmissionSpec, ...] = (SubmissionSpec(1, _DEADLINE),),
    history: tuple[HistorySpec, ...] = (),
) -> Seed:
    async with factory() as session:
        teacher = User(
            username=f"t{run}",
            password_hash=_PASSWORD_HASH,
            nickname=f"老师{run[-4:]}",
            phone_e164=None,
            role=Role.TEACHER,
            status=UserStatus.ACTIVE,
        )
        student = User(
            username=f"2025{run}001",
            password_hash=_PASSWORD_HASH,
            nickname=f"同学{run[-4:]}",
            phone_e164=None,
            role=Role.STUDENT,
            status=UserStatus.ACTIVE,
        )
        session.add_all((teacher, student))
        await session.flush()
        task = Task(
            owner_teacher_id=teacher.id,
            title="小红书考研经验帖数据采集",
            description="采集指定关键词下的笔记正文与互动数据。",
            task_type=TaskType.DATA_CRAWL,
            rarity=TaskRarity.NORMAL,
            base_reward_points=100,
            status=TaskStatus.PUBLISHED,
            deadline_mode=DeadlineMode.RELATIVE,
            duration_minutes=4320,
            submission_schema={
                "required_columns": [
                    {"name": "url", "type": "string", "unique": True},
                    {"name": "title", "type": "string"},
                ]
            },
            submission_schema_version=1,
            allowed_file_types=["CSV", "XLSX"],
            max_file_size_bytes=10 * 1024 * 1024,
            notification_channels=["SMS"],
        )
        session.add(task)
        await session.flush()
        assignment = Assignment(
            task_id=task.id,
            platform="xiaohongshu",
            keyword=f"考研{run[-4:]}",
            availability_status=AssignmentAvailability.OCCUPIED,
        )
        session.add(assignment)
        await session.flush()
        claim = AssignmentClaim(
            assignment_id=assignment.id,
            task_id=task.id,
            user_id=student.id,
            status=claim_status.value,
            claimed_at=_CLAIMED_AT,
            deadline_at=_DEADLINE,
            grace_deadline_at=_GRACE,
            reward_policy_snapshot={"version": 1},
            base_reward_points_snapshot=100,
            submission_schema_version=1,
            reward_lock_status=reward_lock_status.value,
            reward_tier_locked=reward_tier_locked,
            locked_reward_points=locked_reward_points,
            reward_locked_at=reward_locked_at,
            terminal_at=_GRACE if claim_status in _TERMINAL_CLAIM_STATUSES else None,
        )
        session.add(claim)
        await session.flush()
        rows: dict[int, Submission] = {}
        for spec in submissions:
            row = Submission(
                claim_id=claim.id,
                version=spec.version,
                object_key=f"submissions/{claim.id}/{uuid4()}",
                original_filename="数据.csv",
                declared_type="CSV",
                file_size=128,
                submitted_at=spec.submitted_at,
                validation_status=spec.validation_status,
                retention_until=spec.submitted_at + timedelta(days=180),
            )
            session.add(row)
            await session.flush()
            rows[spec.version] = row
        if latest_version is not None:
            claim.latest_submission_id = rows[latest_version].id
        for spec in history:
            session.add(
                RewardLockHistory(
                    claim_id=claim.id,
                    submission_id=rows[spec.version].id,
                    lock_status_from=spec.lock_status_from,
                    lock_status_to=spec.lock_status_to,
                    reward_tier_locked=spec.reward_tier_locked,
                    locked_reward_points=spec.locked_reward_points,
                    reason="直连种子历史行（任务 8 测试前置条件）",
                )
            )
        await session.commit()
        return Seed(
            teacher=teacher,
            student=student,
            task=task,
            assignment=assignment,
            claim=claim,
            submissions=rows,
        )


async def _cleanup(
    factory: async_sessionmaker[AsyncSession],
    *,
    task_ids: list[UUID],
    user_ids: list[UUID],
) -> None:
    async with factory() as session:
        if task_ids:
            claim_ids = select(AssignmentClaim.id).where(
                AssignmentClaim.task_id.in_(task_ids)
            )
            await session.execute(
                delete(RewardLockHistory).where(
                    RewardLockHistory.claim_id.in_(claim_ids)
                )
            )
            await session.execute(
                delete(SubmissionValidation).where(
                    SubmissionValidation.submission_id.in_(
                        select(Submission.id).where(Submission.claim_id.in_(claim_ids))
                    )
                )
            )
            await session.execute(
                delete(Submission).where(Submission.claim_id.in_(claim_ids))
            )
            await session.execute(
                delete(AssignmentClaim).where(AssignmentClaim.task_id.in_(task_ids))
            )
            await session.execute(
                delete(Assignment).where(Assignment.task_id.in_(task_ids))
            )
            await session.execute(delete(Task).where(Task.id.in_(task_ids)))
        if user_ids:
            await session.execute(delete(User).where(User.id.in_(user_ids)))
        await session.commit()


def _service() -> tuple[RewardLockService, InMemoryEventCollector]:
    collector = InMemoryEventCollector()
    service = RewardLockService(clock=FrozenClock(_NOW), events=collector)
    return service, collector


def _lock_reward(
    service: RewardLockService,
    factory: async_sessionmaker[AsyncSession],
    submission_id: UUID,
) -> AssignmentClaim:
    """Run one service call on its own session inside its own loop."""

    async def _call() -> AssignmentClaim:
        async with factory() as session:
            return await service.on_validation_passed(session, submission_id)

    return asyncio.run(_call())


async def _history_rows(
    factory: async_sessionmaker[AsyncSession], claim_id: UUID
) -> list[RewardLockHistory]:
    """No ordering is imposed: rows written in one transaction share a
    ``created_at`` and carry random UUID primary keys, so callers assert
    on counts/predicates, never retrieval order."""
    async with factory() as session:
        rows = await session.execute(
            select(RewardLockHistory).where(RewardLockHistory.claim_id == claim_id)
        )
        return list(rows.scalars().all())


async def _reload(
    factory: async_sessionmaker[AsyncSession], claim_id: UUID
) -> tuple[AssignmentClaim, Assignment]:
    async with factory() as session:
        claim = await session.get(AssignmentClaim, claim_id)
        assert claim is not None
        assignment = await session.get(Assignment, claim.assignment_id)
        assert assignment is not None
        return claim, assignment


# --- the §26 expiry candidate (test-local; plan 07 owns the real worker) --------------


async def _expiry_candidate(
    db: AsyncSession,
    *,
    claim_id: UUID,
    now: datetime,
    require_no_valid_submission: bool = True,
) -> tuple[AssignmentClaim, bool]:
    """Test-local replication of the documented §26 expiry-worker
    pattern: lock the claim FOR UPDATE, re-check INSIDE the transaction,
    then Claim -> EXPIRED + Assignment -> AVAILABLE.

    The faithful recheck (``require_no_valid_submission=True``) mirrors
    §26 verbatim: expire only when the claim is still in a
    student-action status, ``now`` is past grace, AND no VALIDATED
    submission exists under the claim — the arm that implements §11.5's
    "an in-window machine-passed submission protects the claim". The
    aggressive probe (``require_no_valid_submission=False``) drops that
    arm on purpose: it forces the hostile ordering where the worker
    expires the claim despite the (already committed) valid submission,
    so the reward-lock side's terminal-claim no-op is exercised under
    real concurrency rather than only sequentially.

    Returns the claim row and whether THIS call performed the expiry.
    """
    claim = await db.scalar(
        select(AssignmentClaim)
        .where(AssignmentClaim.id == claim_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert claim is not None
    status = ClaimStatus(claim.status)
    actionable = status in (ClaimStatus.CLAIMED, ClaimStatus.REVISION_REQUIRED)
    has_valid_submission = False
    if require_no_valid_submission:
        has_valid_submission = (
            await db.scalar(
                select(Submission.id)
                .where(
                    Submission.claim_id == claim.id,
                    Submission.validation_status == ValidationStatus.VALIDATED.value,
                )
                .limit(1)
            )
            is not None
        )
    expired = False
    if actionable and now >= claim.grace_deadline_at and not has_valid_submission:
        assignment = await db.scalar(
            select(Assignment)
            .where(Assignment.id == claim.assignment_id)
            .with_for_update()
        )
        if (
            assignment is not None
            and AssignmentAvailability(assignment.availability_status)
            is AssignmentAvailability.OCCUPIED
        ):
            assignment.availability_status = AssignmentAvailability.AVAILABLE
        claim.status = ClaimStatus.EXPIRED.value
        claim.terminal_at = now
        expired = True
    await db.commit()
    return claim, expired


async def _race(
    factory: async_sessionmaker[AsyncSession],
    service: RewardLockService,
    *,
    submission_id: UUID,
    claim_id: UUID,
    now: datetime,
    require_no_valid_submission: bool = True,
) -> tuple[AssignmentClaim, tuple[AssignmentClaim, bool]]:
    """Launch the reward-lock transaction and the expiry candidate
    concurrently: both coroutines park on a 2-party barrier inside ONE
    event loop, then race for the claim-row FOR UPDATE on independent
    sessions/connections. PostgreSQL serializes them; exactly one can
    transition the claim.

    A per-side jitter of a few milliseconds follows the barrier — the
    lock side's anchor read costs it one round trip before the claim
    lock, so without jitter the arrival order would be deterministic
    and only one interleaving would ever be exercised. The assertions
    around every race are legal-outcome based, so the jitter cannot
    flake the tests; it only decides WHICH legal outcome each iteration
    produces."""
    barrier = asyncio.Barrier(2)

    async def lock_side() -> AssignmentClaim:
        async with factory() as session:
            await barrier.wait()
            await asyncio.sleep(random.uniform(0, 0.005))
            return await service.on_validation_passed(session, submission_id)

    async def expire_side() -> tuple[AssignmentClaim, bool]:
        async with factory() as session:
            await barrier.wait()
            await asyncio.sleep(random.uniform(0, 0.005))
            return await _expiry_candidate(
                session,
                claim_id=claim_id,
                now=now,
                require_no_valid_submission=require_no_valid_submission,
            )

    return await asyncio.gather(lock_side(), expire_side())


# --- the lock semantics (spec §11.2) --------------------------------------------------


@pytest.mark.integration
def test_first_valid_on_time_submission_locks_provisional_at_full() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = asyncio.run(_seed(factory, run))
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        service, collector = _service()

        # submitted_at exactly at the deadline -> the 100% tier (§9.3
        # inclusive edge), even though the service clock is past grace.
        claim = _lock_reward(service, factory, seed.submissions[1].id)

        assert claim.status == ClaimStatus.UNDER_REVIEW.value
        assert claim.latest_submission_id == seed.submissions[1].id
        assert claim.reward_lock_status == RewardLockStatus.PROVISIONAL.value
        assert claim.reward_tier_locked == 100
        assert claim.locked_reward_points == 100
        assert claim.reward_locked_at == _DEADLINE  # the submit instant, not now
        assert claim.terminal_at is None

        rows = asyncio.run(_history_rows(factory, seed.claim.id))
        assert len(rows) == 1
        assert rows[0].lock_status_from == RewardLockStatus.NONE.value
        assert rows[0].lock_status_to == RewardLockStatus.PROVISIONAL.value
        assert rows[0].submission_id == seed.submissions[1].id
        assert rows[0].reward_tier_locked == 100
        assert rows[0].locked_reward_points == 100

        events = collector.of_type(REWARD_LOCKED)
        assert len(events) == 1
        assert events[0].aggregate_id == seed.claim.id
        assert events[0].payload["reward_tier_locked"] == 100
        assert events[0].payload["locked_reward_points"] == 100

        # The assignment is never touched by the lock flow.
        _, assignment = asyncio.run(_reload(factory, seed.claim.id))
        assert assignment.availability_status == AssignmentAvailability.OCCUPIED.value
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


@pytest.mark.integration
def test_first_valid_at_plus_two_hours_locks_eighty_percent() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        # VALIDATING is the realistic worker-path entry status (the
        # validation service's tx1 moves CLAIMED -> VALIDATING at start).
        seed = asyncio.run(
            _seed(
                factory,
                run,
                claim_status=ClaimStatus.VALIDATING,
                submissions=(SubmissionSpec(1, _DEADLINE + timedelta(hours=2)),),
            )
        )
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        service, collector = _service()

        claim = _lock_reward(service, factory, seed.submissions[1].id)

        assert claim.status == ClaimStatus.UNDER_REVIEW.value
        assert claim.reward_lock_status == RewardLockStatus.PROVISIONAL.value
        assert claim.reward_tier_locked == 80
        assert claim.locked_reward_points == 80
        assert claim.reward_locked_at == _DEADLINE + timedelta(hours=2)

        rows = asyncio.run(_history_rows(factory, seed.claim.id))
        assert len(rows) == 1
        assert rows[0].reward_tier_locked == 80
        assert rows[0].locked_reward_points == 80
        assert len(collector.of_type(REWARD_LOCKED)) == 1
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


@pytest.mark.integration
def test_later_valid_revision_does_not_lower_existing_lock() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = asyncio.run(
            _seed(
                factory,
                run,
                submissions=(
                    SubmissionSpec(1, _DEADLINE),
                    # A deep-late revision (deadline + 18h -> the 20%
                    # tier): must not lower the 100% lock v1 earned.
                    SubmissionSpec(2, _DEADLINE + timedelta(hours=18)),
                ),
            )
        )
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        service, collector = _service()

        first = _lock_reward(service, factory, seed.submissions[1].id)
        assert first.reward_tier_locked == 100
        second = _lock_reward(service, factory, seed.submissions[2].id)

        # The existing lock is untouched — same tier, same points, same
        # locked instant — but the claim now points at the new version.
        assert second.reward_lock_status == RewardLockStatus.PROVISIONAL.value
        assert second.reward_tier_locked == 100
        assert second.locked_reward_points == 100
        assert second.reward_locked_at == _DEADLINE
        assert second.latest_submission_id == seed.submissions[2].id
        assert second.status == ClaimStatus.UNDER_REVIEW.value

        rows = asyncio.run(_history_rows(factory, seed.claim.id))
        assert len(rows) == 1  # no second lock transition, no history rewrite
        assert rows[0].submission_id == seed.submissions[1].id
        assert len(collector.of_type(REWARD_LOCKED)) == 1  # no second event
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


@pytest.mark.integration
def test_confirmed_lock_is_preserved_through_new_valid_submission() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = asyncio.run(
            _seed(
                factory,
                run,
                claim_status=ClaimStatus.REVISION_REQUIRED,
                reward_lock_status=RewardLockStatus.CONFIRMED,
                reward_tier_locked=100,
                locked_reward_points=100,
                reward_locked_at=_DEADLINE,
                latest_version=1,
                submissions=(
                    SubmissionSpec(1, _DEADLINE),
                    SubmissionSpec(2, _DEADLINE + timedelta(hours=2)),
                ),
            )
        )
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        service, collector = _service()

        claim = _lock_reward(service, factory, seed.submissions[2].id)

        # CONFIRMED is never touched; the claim still moves on to review
        # and the latest pointer advances.
        assert claim.reward_lock_status == RewardLockStatus.CONFIRMED.value
        assert claim.reward_tier_locked == 100
        assert claim.locked_reward_points == 100
        assert claim.reward_locked_at == _DEADLINE
        assert claim.status == ClaimStatus.UNDER_REVIEW.value
        assert claim.latest_submission_id == seed.submissions[2].id
        assert asyncio.run(_history_rows(factory, seed.claim.id)) == []
        assert collector.of_type(REWARD_LOCKED) == []
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


@pytest.mark.integration
def test_invalidated_lock_permits_new_provisional_at_new_fraction() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        # The task-9 precondition, direct-seeded: v1 locked PROVISIONAL
        # at 100%, a reviewer then invalidated the lock. The projection
        # columns read INVALIDATED with no active lock values.
        seed = asyncio.run(
            _seed(
                factory,
                run,
                reward_lock_status=RewardLockStatus.INVALIDATED,
                submissions=(
                    SubmissionSpec(1, _DEADLINE),
                    SubmissionSpec(2, _DEADLINE + timedelta(hours=2)),
                ),
                history=(
                    HistorySpec(
                        RewardLockStatus.NONE.value,
                        RewardLockStatus.PROVISIONAL.value,
                        version=1,
                        reward_tier_locked=100,
                        locked_reward_points=100,
                    ),
                    HistorySpec(
                        RewardLockStatus.PROVISIONAL.value,
                        RewardLockStatus.INVALIDATED.value,
                        version=1,
                    ),
                ),
            )
        )
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        service, collector = _service()

        claim = _lock_reward(service, factory, seed.submissions[2].id)

        # A NEW PROVISIONAL lock at v2's own fraction; the invalidated
        # audit rows survive untouched below it.
        assert claim.reward_lock_status == RewardLockStatus.PROVISIONAL.value
        assert claim.reward_tier_locked == 80
        assert claim.locked_reward_points == 80
        assert claim.reward_locked_at == _DEADLINE + timedelta(hours=2)
        assert claim.status == ClaimStatus.UNDER_REVIEW.value
        assert claim.latest_submission_id == seed.submissions[2].id

        rows = asyncio.run(_history_rows(factory, seed.claim.id))
        # The rows share one transaction's created_at and carry random
        # UUIDs, so no retrieval order exists — assert the transition
        # multiset plus the new row's identity.
        assert sorted(
            (row.lock_status_from, row.lock_status_to) for row in rows
        ) == sorted(
            (
                (RewardLockStatus.NONE.value, RewardLockStatus.PROVISIONAL.value),
                (
                    RewardLockStatus.PROVISIONAL.value,
                    RewardLockStatus.INVALIDATED.value,
                ),
                (
                    RewardLockStatus.INVALIDATED.value,
                    RewardLockStatus.PROVISIONAL.value,
                ),
            )
        )
        relock = [
            row
            for row in rows
            if row.lock_status_from == RewardLockStatus.INVALIDATED.value
            and row.lock_status_to == RewardLockStatus.PROVISIONAL.value
        ]
        assert len(relock) == 1
        assert relock[0].submission_id == seed.submissions[2].id
        assert relock[0].reward_tier_locked == 80
        assert relock[0].locked_reward_points == 80
        assert len(collector.of_type(REWARD_LOCKED)) == 1
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


@pytest.mark.integration
def test_replay_returns_current_claim_without_duplicate_writes() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = asyncio.run(_seed(factory, run))
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        service, collector = _service()

        first = _lock_reward(service, factory, seed.submissions[1].id)
        replay = _lock_reward(service, factory, seed.submissions[1].id)

        assert replay.status == ClaimStatus.UNDER_REVIEW.value
        assert replay.reward_lock_status == RewardLockStatus.PROVISIONAL.value
        assert replay.reward_locked_at == first.reward_locked_at
        assert len(asyncio.run(_history_rows(factory, seed.claim.id))) == 1
        assert len(collector.of_type(REWARD_LOCKED)) == 1
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


@pytest.mark.integration
def test_terminal_claim_is_an_idempotent_noop() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = asyncio.run(_seed(factory, run, claim_status=ClaimStatus.EXPIRED))
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        service, collector = _service()

        claim = _lock_reward(service, factory, seed.submissions[1].id)

        # The expiry-won ordering: the terminal claim is returned
        # unchanged — never resurrected, never locked, no event.
        assert claim.status == ClaimStatus.EXPIRED.value
        assert claim.terminal_at == _GRACE
        assert claim.reward_lock_status == RewardLockStatus.NONE.value
        assert claim.latest_submission_id is None
        assert asyncio.run(_history_rows(factory, seed.claim.id)) == []
        assert collector.of_type(REWARD_LOCKED) == []
        _, assignment = asyncio.run(_reload(factory, seed.claim.id))
        assert assignment.availability_status == AssignmentAvailability.OCCUPIED.value
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


# --- corrupted / rejected preconditions ----------------------------------------------


@pytest.mark.integration
def test_validated_submission_past_grace_is_typed_error_without_transition() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        # Data corruption shape: the submission is VALIDATED but its
        # submitted_at sits at/after grace — finalize and validation
        # both passed a window that is now closed. RewardWindowInconsistentError
        # carries SUBMISSION_WINDOW_CLOSED; the claim is NOT transitioned.
        seed = asyncio.run(
            _seed(
                factory,
                run,
                submissions=(SubmissionSpec(1, _GRACE + timedelta(milliseconds=1)),),
            )
        )
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        service, collector = _service()

        with pytest.raises(RewardWindowInconsistentError) as excinfo:
            _lock_reward(service, factory, seed.submissions[1].id)
        assert excinfo.value.code is ErrorCode.SUBMISSION_WINDOW_CLOSED

        claim, _ = asyncio.run(_reload(factory, seed.claim.id))
        assert claim.status == ClaimStatus.CLAIMED.value
        assert claim.reward_lock_status == RewardLockStatus.NONE.value
        assert claim.latest_submission_id is None
        assert asyncio.run(_history_rows(factory, seed.claim.id)) == []
        assert collector.of_type(REWARD_LOCKED) == []
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


@pytest.mark.integration
def test_not_validated_submission_is_rejected() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = asyncio.run(
            _seed(
                factory,
                run,
                submissions=(
                    SubmissionSpec(
                        1, _DEADLINE, validation_status=ValidationStatus.UPLOADED.value
                    ),
                ),
            )
        )
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        service, _collector = _service()

        with pytest.raises(BusinessError) as excinfo:
            _lock_reward(service, factory, seed.submissions[1].id)
        assert isinstance(excinfo.value, SubmissionNotValidatedError)

        claim, _ = asyncio.run(_reload(factory, seed.claim.id))
        assert claim.status == ClaimStatus.CLAIMED.value
        assert claim.reward_lock_status == RewardLockStatus.NONE.value
        assert asyncio.run(_history_rows(factory, seed.claim.id)) == []
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


@pytest.mark.integration
def test_missing_submission_raises_not_found() -> None:
    factory = _new_factory()
    service, _collector = _service()
    with pytest.raises(BusinessError) as excinfo:
        _lock_reward(service, factory, uuid4())
    assert excinfo.value.code is ErrorCode.NOT_FOUND


# --- the submit-vs-expire race at the grace boundary (§26) ----------------------------


@pytest.mark.integration
def test_race_validation_pass_vs_expiry_candidate_at_grace_boundary() -> None:
    """The review-focus race (§26): the submission finalized at
    grace-1ms and is machine-VALIDATED; the expiry worker's scan fires
    at grace+1ms. Both transactions race for the claim row.

    Against the FAITHFUL §26 candidate (whose recheck includes the
    no-valid-submission arm), only ONE legal outcome exists in this
    shape: the reward-lock transaction wins, the claim is UNDER_REVIEW
    with a PROVISIONAL lock at the boundary tier (grace-1ms is the 20%
    tier), and the Assignment is NEVER released (§11.5: an in-window
    machine-passed submission protects the claim). The disjunctive
    contract — claim UNDER_REVIEW + assignment OCCUPIED + lock, OR claim
    EXPIRED + assignment AVAILABLE + no lock, never mixed — is pinned by
    the assertions below; the expiry disjunct is unreachable by
    construction (the candidate's own recheck) and its lock-side no-op
    arm is exercised concurrently in the companion race test.

    Five consecutive fresh-seed iterations for scheduling stability.
    """
    factory = _new_factory()
    service, collector = _service()
    for iteration in range(5):
        run = uuid4().hex[:8]
        task_ids: list[UUID] = []
        user_ids: list[UUID] = []
        try:
            seed = asyncio.run(
                _seed(
                    factory,
                    f"{run}{iteration}",
                    submissions=(
                        SubmissionSpec(1, _GRACE - timedelta(milliseconds=1)),
                    ),
                )
            )
            task_ids.append(seed.task.id)
            user_ids.extend((seed.teacher.id, seed.student.id))

            locked, (probe_claim, probe_expired) = asyncio.run(
                _race(
                    factory,
                    service,
                    submission_id=seed.submissions[1].id,
                    claim_id=seed.claim.id,
                    now=_NOW,
                )
            )

            # The faithful candidate re-checked under the claim lock and
            # found the VALIDATED submission: it never expires — it
            # either saw the claim already moved to UNDER_REVIEW (the
            # lock side won the row first) or still CLAIMED.
            assert probe_expired is False
            assert probe_claim.status != ClaimStatus.EXPIRED.value

            # The lock side won: protected outcome, no mixed state.
            assert locked.status == ClaimStatus.UNDER_REVIEW.value
            claim, assignment = asyncio.run(_reload(factory, seed.claim.id))
            assert claim.status == ClaimStatus.UNDER_REVIEW.value
            assert claim.terminal_at is None
            assert claim.reward_lock_status == RewardLockStatus.PROVISIONAL.value
            assert claim.reward_tier_locked == 20  # grace-1ms is the late tier
            assert claim.locked_reward_points == 20
            assert claim.latest_submission_id == seed.submissions[1].id
            # The invariant: the assignment must NOT become AVAILABLE.
            assert (
                assignment.availability_status == AssignmentAvailability.OCCUPIED.value
            )
            rows = asyncio.run(_history_rows(factory, seed.claim.id))
            assert len(rows) == 1
            assert rows[0].lock_status_to == RewardLockStatus.PROVISIONAL.value
            assert len(collector.of_type(REWARD_LOCKED)) == iteration + 1
        finally:
            asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


@pytest.mark.integration
def test_race_expiry_first_then_reward_lock_noops_idempotently() -> None:
    """The hostile ordering of the §26 race, forced by an AGGRESSIVE
    expiry probe that omits the no-valid-submission recheck arm (the
    worker shape that scanned before the VALIDATED commit landed for
    it). Both orderings are now reachable, and whichever wins, the final
    state must be exactly one of the two legal outcomes — never the
    catastrophic mixtures (UNDER_REVIEW + AVAILABLE, or EXPIRED with a
    PROVISIONAL lock).

    When the probe wins, the reward-lock transaction acquires the claim
    lock afterwards, sees the terminal claim, and no-ops idempotently:
    the claim stays EXPIRED with no lock, no history, no resurrection.
    Five consecutive fresh-seed iterations for scheduling stability.
    """
    factory = _new_factory()
    service, collector = _service()
    outcomes: list[str] = []
    for iteration in range(5):
        run = uuid4().hex[:8]
        task_ids: list[UUID] = []
        user_ids: list[UUID] = []
        try:
            seed = asyncio.run(
                _seed(
                    factory,
                    f"{run}{iteration}",
                    submissions=(
                        SubmissionSpec(1, _GRACE - timedelta(milliseconds=1)),
                    ),
                )
            )
            task_ids.append(seed.task.id)
            user_ids.extend((seed.teacher.id, seed.student.id))

            locked, (_probe_claim, probe_expired) = asyncio.run(
                _race(
                    factory,
                    service,
                    submission_id=seed.submissions[1].id,
                    claim_id=seed.claim.id,
                    now=_NOW,
                    require_no_valid_submission=False,
                )
            )

            claim, assignment = asyncio.run(_reload(factory, seed.claim.id))
            rows = asyncio.run(_history_rows(factory, seed.claim.id))
            if probe_expired:
                outcomes.append("expired")
                # Expiry won first: the lock side saw the terminal claim
                # and no-opped — the claim it returned is the terminal
                # row itself, and nothing was written on top.
                assert locked.status == ClaimStatus.EXPIRED.value
                assert claim.status == ClaimStatus.EXPIRED.value
                assert claim.terminal_at == _NOW
                assert claim.reward_lock_status == RewardLockStatus.NONE.value
                assert claim.latest_submission_id is None
                assert rows == []
                assert (
                    assignment.availability_status
                    == AssignmentAvailability.AVAILABLE.value
                )
            else:
                outcomes.append("locked")
                # The lock side won the claim row first: the probe then
                # re-checked UNDER_REVIEW (not student-actionable) and
                # stood down; the assignment is never released.
                assert locked.status == ClaimStatus.UNDER_REVIEW.value
                assert claim.status == ClaimStatus.UNDER_REVIEW.value
                assert claim.terminal_at is None
                assert claim.reward_lock_status == RewardLockStatus.PROVISIONAL.value
                assert claim.reward_tier_locked == 20
                assert (
                    assignment.availability_status
                    == AssignmentAvailability.OCCUPIED.value
                )
                assert len(rows) == 1
            assert len(collector.of_type(REWARD_LOCKED)) == outcomes.count("locked")
        finally:
            asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))
    # Both orderings stay legal across iterations; the distribution
    # itself is scheduler property, not a contract.
    assert len(outcomes) == 5
