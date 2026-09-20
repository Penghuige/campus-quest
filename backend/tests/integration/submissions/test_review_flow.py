# backend/tests/integration/submissions/test_review_flow.py
"""Human review flow: revision window, lock invalidation, approve
(spec §11.3, §11.4, §14, §30; plan 04 task 9).

Covers ``ReviewService`` (require_revision / invalidate_reward_lock /
approve_submission) plus the §11.3 x §11.4 re-lock clamp in
``RewardLockService.on_validation_passed``:

- the §11.4 revision window: a late review (two days past grace) sets
  ``revision_deadline_at = reviewed_at + 24h`` and PRESERVES the 100%
  lock untouched; a review before the deadline keeps grace as the
  deadline (the max() formula's other arm); a re-退回 recomputes from
  the NEW reviewed_at and the deadline extends monotonically;
- the §11.3 invalidation: reason mandatory, the provisional lock is
  cancelled (projection cleared, append-only history + review rows +
  audit event record the previous values), the claim moves to
  REVISION_REQUIRED, and a later valid submission re-locks at ITS own
  submitted_at fraction (deadline + 7h -> 50%);
- the controller-ruling clamp: INVALIDATED + a revision-window
  submission at grace + 25h re-locks PROVISIONAL at the lowest defined
  tier 20% — not WindowClosed, not a 500;
- the §14 approve transaction: all ten effects in ONE transaction
  (submission APPROVED, claim COMPLETED + terminal_at, lock CONFIRMED
  + history, the frozen PointsRewardPort grant with a stable
  idempotency key, assignment COMPLETED, the audit event), and two
  concurrent approves grant EXACTLY once (row-lock arbitration +
  terminal gate; the second caller gets the idempotent ALREADY_REVIEWED
  result);
- the gates: permission matrix (owner / REVIEW_SUBMISSIONS
  collaborator / Admin allowed; VIEW_TASK-only collaborator, unrelated
  teacher, and the student denied), permission judged BEFORE the
  approve lock-state gate (an unauthorized teacher never learns the
  lock shape), stale versions rejected, terminal claims rejected,
  CONFIRMED/NONE locks not invalidatable, approve requires a
  PROVISIONAL lock, and a RETIRED/pre-COMPLETED assignment is sticky
  under approve (no-op flip, claim still completes).

Harness notes (same conventions as test_reward_lock.py): explicit
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
from app.modules.identity.events import Actor, InMemoryEventCollector
from app.modules.identity.models import User
from app.modules.submissions.enums import ReviewAction, ValidationStatus
from app.modules.submissions.models import (
    RewardLockHistory,
    Submission,
    SubmissionReview,
)
from app.modules.submissions.review_service import (
    REVISION_REQUIRED_EVENT,
    REWARD_LOCK_INVALIDATED_EVENT,
    SUBMISSION_APPROVED_EVENT,
    ApprovalResult,
    ClaimNotReviewableError,
    InvalidationReasonRequiredError,
    LockNotInvalidatableError,
    LockNotProvisionalError,
    ReviewerPermissionDeniedError,
    ReviewService,
    StaleSubmissionVersionError,
)
from app.modules.submissions.reward_lock_service import (
    REWARD_LOCKED,
    RewardLockService,
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
from app.modules.tasks.models import (
    Assignment,
    AssignmentClaim,
    Task,
    TaskCollaborator,
)
from tests.fakes.points import FakePointsRewardPort

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)
# The submission window: deadline at 09:00, grace 24h later (spec §9.1).
_DEADLINE = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
_GRACE = _DEADLINE + timedelta(hours=24)
_CLAIMED_AT = _DEADLINE - timedelta(days=3)
# A review instant comfortably inside the window for the default tests.
_REVIEWED_AT = _DEADLINE + timedelta(hours=2)


@dataclass(slots=True)
class Seed:
    owner: User
    student: User
    admin: User
    teacher_review: User
    teacher_view: User
    teacher_other: User
    task: Task
    assignment: Assignment
    claim: AssignmentClaim
    submissions: dict[int, Submission]

    def actor(self, user: User, role: Role | None = None) -> Actor:
        return Actor(
            user_id=user.id, role=role if role is not None else Role(user.role)
        )


@dataclass(frozen=True, slots=True)
class SubmissionSpec:
    version: int
    submitted_at: datetime
    validation_status: str = ValidationStatus.VALIDATED.value
    review_status: str = "PENDING_REVIEW"


@dataclass(frozen=True, slots=True)
class HistorySpec:
    """One direct-seeded RewardLockHistory row (a precondition)."""

    lock_status_from: str | None
    lock_status_to: str
    version: int
    reward_tier_locked: int | None = None
    locked_reward_points: int | None = None
    changed_by: UUID | None = None
    reason: str | None = None


def _new_factory() -> async_sessionmaker[AsyncSession]:
    """NullPool session factory: fresh connection per checkout, so
    asyncio.run phases on fresh loops never share a pooled connection."""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False)


def _user(run: str, prefix: str, role: Role) -> User:
    return User(
        username=f"{prefix}{run}",
        password_hash=_PASSWORD_HASH,
        nickname=f"{prefix}师{run[-4:]}"
        if role is not Role.STUDENT
        else f"同学{run[-4:]}",
        phone_e164=None,
        role=role,
        status=UserStatus.ACTIVE,
    )


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    run: str,
    *,
    claim_status: ClaimStatus = ClaimStatus.UNDER_REVIEW,
    reward_lock_status: RewardLockStatus = RewardLockStatus.PROVISIONAL,
    reward_tier_locked: int | None = 100,
    locked_reward_points: int | None = 100,
    reward_locked_at: datetime | None = _DEADLINE,
    revision_deadline_at: datetime | None = None,
    latest_version: int | None = 1,
    assignment_status: AssignmentAvailability = AssignmentAvailability.OCCUPIED,
    submissions: tuple[SubmissionSpec, ...] = (SubmissionSpec(1, _DEADLINE),),
    history: tuple[HistorySpec, ...] = (),
) -> Seed:
    """One reviewable world: owner + student + permission cast, a
    PUBLISHED task with the two collaborator rows the matrix needs, an
    OCCUPIED assignment, a claim in the given lock/review state, and the
    submission versions (latest pointer at ``latest_version``)."""

    async with factory() as session:
        owner = _user(run, "t", Role.TEACHER)
        student = _user(run, "2025s", Role.STUDENT)
        admin = _user(run, "a", Role.ADMIN)
        teacher_review = _user(run, "tr", Role.TEACHER)
        teacher_view = _user(run, "tv", Role.TEACHER)
        teacher_other = _user(run, "to", Role.TEACHER)
        session.add_all(
            (owner, student, admin, teacher_review, teacher_view, teacher_other)
        )
        await session.flush()
        task = Task(
            owner_teacher_id=owner.id,
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
        session.add_all(
            (
                TaskCollaborator(
                    task_id=task.id,
                    teacher_id=teacher_review.id,
                    permissions=["REVIEW_SUBMISSIONS"],
                ),
                TaskCollaborator(
                    task_id=task.id,
                    teacher_id=teacher_view.id,
                    permissions=["VIEW_TASK"],
                ),
            )
        )
        assignment = Assignment(
            task_id=task.id,
            platform="xiaohongshu",
            keyword=f"考研{run[-4:]}",
            availability_status=assignment_status.value,
        )
        session.add(assignment)
        await session.flush()
        terminal = claim_status in (
            ClaimStatus.COMPLETED,
            ClaimStatus.ABANDONED,
            ClaimStatus.EXPIRED,
        )
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
            revision_deadline_at=revision_deadline_at,
            terminal_at=_GRACE if terminal else None,
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
                review_status=spec.review_status,
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
                    changed_by=spec.changed_by,
                    reason=spec.reason,
                )
            )
        await session.commit()
        return Seed(
            owner=owner,
            student=student,
            admin=admin,
            teacher_review=teacher_review,
            teacher_view=teacher_view,
            teacher_other=teacher_other,
            task=task,
            assignment=assignment,
            claim=claim,
            submissions=rows,
        )


async def _add_submission(
    factory: async_sessionmaker[AsyncSession],
    claim_id: UUID,
    spec: SubmissionSpec,
) -> Submission:
    """Insert one more submission version mid-test (the finalize+worker
    outcome when the student resubmits inside the revision window)."""

    async with factory() as session:
        row = Submission(
            claim_id=claim_id,
            version=spec.version,
            object_key=f"submissions/{claim_id}/{uuid4()}",
            original_filename=f"数据v{spec.version}.csv",
            declared_type="CSV",
            file_size=128,
            submitted_at=spec.submitted_at,
            validation_status=spec.validation_status,
            review_status=spec.review_status,
            retention_until=spec.submitted_at + timedelta(days=180),
        )
        session.add(row)
        await session.commit()
        return row


async def _cleanup(
    factory: async_sessionmaker[AsyncSession],
    *,
    task_ids: list[UUID],
    run: str,
) -> None:
    async with factory() as session:
        if task_ids:
            claim_ids = select(AssignmentClaim.id).where(
                AssignmentClaim.task_id.in_(task_ids)
            )
            submission_ids = select(Submission.id).where(
                Submission.claim_id.in_(claim_ids)
            )
            await session.execute(
                delete(SubmissionReview).where(
                    SubmissionReview.submission_id.in_(submission_ids)
                )
            )
            await session.execute(
                delete(RewardLockHistory).where(
                    RewardLockHistory.claim_id.in_(claim_ids)
                )
            )
            await session.execute(
                delete(Submission).where(Submission.claim_id.in_(claim_ids))
            )
            await session.execute(
                delete(TaskCollaborator).where(TaskCollaborator.task_id.in_(task_ids))
            )
            await session.execute(
                delete(AssignmentClaim).where(AssignmentClaim.task_id.in_(task_ids))
            )
            await session.execute(
                delete(Assignment).where(Assignment.task_id.in_(task_ids))
            )
            await session.execute(delete(Task).where(Task.id.in_(task_ids)))
        # ALL seeded users go by the run token: every username this
        # module mints ends with it (prefix + run), so the six-role cast
        # (and anything a future seed adds) is cleaned by construction
        # — the T9 carry fix for the owner+student-only id lists that
        # leaked the other four roles per test.
        await session.execute(delete(User).where(User.username.endswith(run)))
        await session.commit()


def _service(
    now: datetime,
) -> tuple[ReviewService, InMemoryEventCollector, FakePointsRewardPort]:
    collector = InMemoryEventCollector()
    points = FakePointsRewardPort()
    service = ReviewService(clock=FrozenClock(now), events=collector, points=points)
    return service, collector, points


def _call(factory: async_sessionmaker[AsyncSession], fn) -> object:
    """Run one service call on its own session inside its own loop."""

    async def _run() -> object:
        async with factory() as session:
            return await fn(session)

    return asyncio.run(_run())


def _lock_service(
    now: datetime, collector: InMemoryEventCollector | None = None
) -> tuple[RewardLockService, InMemoryEventCollector]:
    events = collector if collector is not None else InMemoryEventCollector()
    return (
        RewardLockService(clock=FrozenClock(now), events=events),
        events,
    )


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


async def _review_rows(
    factory: async_sessionmaker[AsyncSession], submission_id: UUID
) -> list[SubmissionReview]:
    async with factory() as session:
        rows = await session.execute(
            select(SubmissionReview).where(
                SubmissionReview.submission_id == submission_id
            )
        )
        return list(rows.scalars().all())


async def _reload(
    factory: async_sessionmaker[AsyncSession], claim_id: UUID
) -> tuple[AssignmentClaim, Assignment, Submission]:
    async with factory() as session:
        claim = await session.get(AssignmentClaim, claim_id)
        assert claim is not None
        assignment = await session.get(Assignment, claim.assignment_id)
        assert assignment is not None
        assert claim.latest_submission_id is not None
        submission = await session.get(Submission, claim.latest_submission_id)
        assert submission is not None
        return claim, assignment, submission


# --- the §11.4 revision window --------------------------------------------------


@pytest.mark.integration
def test_late_review_sets_revision_deadline_and_preserves_reward() -> None:
    """The brief's late-review test: an on-time submission (100% locked)
    is reviewed two days PAST grace. The revision deadline becomes
    reviewed_at + 24h (later than the original grace), and the reward
    stays 100% — review delay never costs the student the tier (§11.5:
    DDL constrains student behavior, not teacher review time)."""
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    try:
        seed = asyncio.run(_seed(factory, run))
        task_ids.append(seed.task.id)
        reviewed_at = _GRACE + timedelta(days=2)
        service, collector, _points = _service(reviewed_at)

        claim = _call(
            factory,
            lambda session: service.require_revision(
                session,
                seed.actor(seed.owner),
                seed.submissions[1].id,
                "格式问题，请补充来源列。",
            ),
        )
        assert isinstance(claim, AssignmentClaim)
        assert claim.status == ClaimStatus.REVISION_REQUIRED.value
        assert claim.revision_deadline_at == reviewed_at + timedelta(hours=24)
        assert claim.terminal_at is None
        # The lock is preserved untouched (§11.2: 普通质量问题保留档位).
        assert claim.reward_lock_status == RewardLockStatus.PROVISIONAL.value
        assert claim.reward_tier_locked == 100
        assert claim.locked_reward_points == 100
        assert claim.reward_locked_at == _DEADLINE

        _, assignment, submission = asyncio.run(_reload(factory, seed.claim.id))
        assert submission.review_status == "REVISION_REQUIRED"
        assert submission.reviewer_id == seed.owner.id
        assert submission.reviewed_at == reviewed_at
        assert submission.review_note == "格式问题，请补充来源列。"
        assert assignment.availability_status == AssignmentAvailability.OCCUPIED.value

        rows = asyncio.run(_review_rows(factory, seed.submissions[1].id))
        assert len(rows) == 1
        assert rows[0].action == ReviewAction.REQUIRE_REVISION.value
        assert rows[0].reviewer_id == seed.owner.id
        assert rows[0].note == "格式问题，请补充来源列。"
        # No lock transition: revision preserves the lock (§11.3).
        assert asyncio.run(_history_rows(factory, seed.claim.id)) == []

        events = collector.of_type(REVISION_REQUIRED_EVENT)
        assert len(events) == 1
        assert events[0].aggregate_id == seed.claim.id
        assert events[0].occurred_at == reviewed_at
        assert (
            events[0].payload["revision_deadline_at"]
            == (reviewed_at + timedelta(hours=24)).isoformat()
        )
        assert events[0].payload["submission_id"] == str(seed.submissions[1].id)
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, run=run))


@pytest.mark.integration
def test_review_before_deadline_keeps_grace_as_revision_deadline() -> None:
    """The max() formula's other arm (§11.4): reviewing BEFORE the
    deadline yields reviewed_at + 24h < grace, so the original grace
    stays the revision deadline — the window never shrinks below grace."""
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    try:
        seed = asyncio.run(
            _seed(
                factory,
                run,
                submissions=(SubmissionSpec(1, _DEADLINE - timedelta(hours=3)),),
            )
        )
        task_ids.append(seed.task.id)
        reviewed_at = _DEADLINE - timedelta(hours=2)
        service, _collector, _points = _service(reviewed_at)

        claim = _call(
            factory,
            lambda session: service.require_revision(
                session, seed.actor(seed.owner), seed.submissions[1].id, "请补充数据。"
            ),
        )
        assert isinstance(claim, AssignmentClaim)
        assert claim.revision_deadline_at == _GRACE
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, run=run))


@pytest.mark.integration
def test_second_rejection_recomputes_deadline_from_new_reviewed_at() -> None:
    """Re-退回 (§11.4): after a revision and a fresh valid submission
    the claim is back UNDER_REVIEW; a second rejection computes from the
    NEW reviewed_at, and the deadline extends monotonically."""
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    try:
        seed = asyncio.run(_seed(factory, run))
        task_ids.append(seed.task.id)
        first_reviewed_at = _DEADLINE + timedelta(hours=2)
        service1, _collector1, _points1 = _service(first_reviewed_at)

        first = _call(
            factory,
            lambda session: service1.require_revision(
                session, seed.actor(seed.owner), seed.submissions[1].id, "第一轮意见。"
            ),
        )
        assert isinstance(first, AssignmentClaim)
        assert first.revision_deadline_at == first_reviewed_at + timedelta(hours=24)

        # The student resubmits inside the revision window; the worker
        # validates and the reward-lock service moves the claim back to
        # UNDER_REVIEW with the latest pointer on v2 (the lock survives).
        v2 = asyncio.run(
            _add_submission(
                factory,
                seed.claim.id,
                SubmissionSpec(2, _DEADLINE + timedelta(hours=20)),
            )
        )
        lock_service, _lock_collector = _lock_service(
            first_reviewed_at + timedelta(hours=1)
        )
        _call(
            factory, lambda session: lock_service.on_validation_passed(session, v2.id)
        )

        second_reviewed_at = first_reviewed_at + timedelta(hours=5)
        service2, _collector2, _points2 = _service(second_reviewed_at)
        second = _call(
            factory,
            lambda session: service2.require_revision(
                session, seed.actor(seed.owner), v2.id, "第二轮意见。"
            ),
        )
        assert isinstance(second, AssignmentClaim)
        assert second.status == ClaimStatus.REVISION_REQUIRED.value
        assert second.revision_deadline_at == second_reviewed_at + timedelta(
            hours=24
        )  # the NEW reviewed_at, not the previous deadline
        assert (
            second.revision_deadline_at > first.revision_deadline_at  # type: ignore[operator]
        )
        rows = asyncio.run(_review_rows(factory, v2.id))
        assert len(rows) == 1
        assert rows[0].action == ReviewAction.REQUIRE_REVISION.value
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, run=run))


# --- the §11.3 invalidation ------------------------------------------------------


@pytest.mark.integration
def test_invalidate_reward_lock_reason_is_mandatory() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    try:
        seed = asyncio.run(_seed(factory, run))
        task_ids.append(seed.task.id)
        service, collector, _points = _service(_REVIEWED_AT)

        for blank in ("", "   "):
            with pytest.raises(BusinessError) as excinfo:
                _call(
                    factory,
                    lambda session, blank=blank, svc=service, world=seed: (
                        svc.invalidate_reward_lock(
                            session,
                            world.actor(world.owner),
                            world.submissions[1].id,
                            blank,
                        )
                    ),
                )
            assert isinstance(excinfo.value, InvalidationReasonRequiredError)
            assert excinfo.value.code is ErrorCode.VALIDATION_ERROR

        claim, _assignment, submission = asyncio.run(_reload(factory, seed.claim.id))
        assert claim.status == ClaimStatus.UNDER_REVIEW.value
        assert claim.reward_lock_status == RewardLockStatus.PROVISIONAL.value
        assert submission.review_status == "PENDING_REVIEW"
        assert asyncio.run(_history_rows(factory, seed.claim.id)) == []
        assert asyncio.run(_review_rows(factory, seed.submissions[1].id)) == []
        assert collector.events == []
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, run=run))


@pytest.mark.integration
def test_invalidate_cancels_provisional_lock_and_audits() -> None:
    """§11.3 INVALIDATE_REWARD_LOCK: the provisional lock is cancelled
    (projection cleared), the append-only history and review rows plus
    the audit event record the previous values and the reviewer reason,
    and the claim moves to REVISION_REQUIRED with a §11.4 deadline."""
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    try:
        seed = asyncio.run(_seed(factory, run))
        task_ids.append(seed.task.id)
        reviewed_at = _DEADLINE + timedelta(hours=1)
        service, collector, _points = _service(reviewed_at)
        reason = "空壳提交：仅含表头，无任何数据行。"

        claim = _call(
            factory,
            lambda session: service.invalidate_reward_lock(
                session, seed.actor(seed.owner), seed.submissions[1].id, reason
            ),
        )
        assert isinstance(claim, AssignmentClaim)
        assert claim.reward_lock_status == RewardLockStatus.INVALIDATED.value
        assert claim.reward_tier_locked is None
        assert claim.locked_reward_points is None
        assert claim.reward_locked_at is None
        assert claim.status == ClaimStatus.REVISION_REQUIRED.value
        # §11.4 from THIS reviewed_at: deadline+1h+24h > grace.
        assert claim.revision_deadline_at == reviewed_at + timedelta(hours=24)
        assert claim.terminal_at is None

        _, assignment, submission = asyncio.run(_reload(factory, seed.claim.id))
        assert submission.review_status == "REVISION_REQUIRED"
        assert submission.reviewer_id == seed.owner.id
        assert submission.reviewed_at == reviewed_at
        assert submission.review_note == reason
        assert assignment.availability_status == AssignmentAvailability.OCCUPIED.value

        history = asyncio.run(_history_rows(factory, seed.claim.id))
        assert len(history) == 1
        assert history[0].lock_status_from == RewardLockStatus.PROVISIONAL.value
        assert history[0].lock_status_to == RewardLockStatus.INVALIDATED.value
        assert history[0].reward_tier_locked == 100  # the cancelled values survive
        assert history[0].locked_reward_points == 100
        assert history[0].changed_by == seed.owner.id
        assert history[0].reason == reason
        assert history[0].submission_id == seed.submissions[1].id

        rows = asyncio.run(_review_rows(factory, seed.submissions[1].id))
        assert len(rows) == 1
        assert rows[0].action == ReviewAction.INVALIDATE_LOCK.value
        assert rows[0].note == reason

        events = collector.of_type(REWARD_LOCK_INVALIDATED_EVENT)
        assert len(events) == 1
        assert events[0].aggregate_id == seed.claim.id
        assert events[0].payload["reason"] == reason
        assert events[0].payload["reviewer_id"] == str(seed.owner.id)
        assert events[0].payload["previous_reward_tier_locked"] == 100
        assert events[0].payload["previous_locked_reward_points"] == 100
        assert (
            events[0].payload["lock_status_from"] == RewardLockStatus.PROVISIONAL.value
        )
        assert events[0].payload["lock_status_to"] == RewardLockStatus.INVALIDATED.value
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, run=run))


@pytest.mark.integration
def test_later_valid_submission_after_invalidation_relocks_at_own_tier() -> None:
    """The brief's invalidation continuation: after the invalidation the
    student resubmits; a valid submission 7h past the deadline re-locks
    PROVISIONAL at the 50% tier from ITS submitted_at (§11.3)."""
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    try:
        seed = asyncio.run(_seed(factory, run))
        task_ids.append(seed.task.id)
        reviewed_at = _DEADLINE + timedelta(hours=1)
        service, _collector, _points = _service(reviewed_at)
        claim = _call(
            factory,
            lambda session: service.invalidate_reward_lock(
                session, seed.actor(seed.owner), seed.submissions[1].id, "空壳提交。"
            ),
        )
        assert isinstance(claim, AssignmentClaim)
        assert claim.reward_lock_status == RewardLockStatus.INVALIDATED.value

        # The student resubmits at deadline + 7h — inside the revision
        # window (deadline + 25h) and inside grace, so the §9.3 ladder
        # answers 50% directly.
        v2 = asyncio.run(
            _add_submission(
                factory,
                seed.claim.id,
                SubmissionSpec(2, _DEADLINE + timedelta(hours=7)),
            )
        )
        lock_service, lock_collector = _lock_service(reviewed_at + timedelta(hours=6))
        relocked = _call(
            factory,
            lambda session: lock_service.on_validation_passed(session, v2.id),
        )
        assert isinstance(relocked, AssignmentClaim)
        assert relocked.reward_lock_status == RewardLockStatus.PROVISIONAL.value
        assert relocked.reward_tier_locked == 50
        assert relocked.locked_reward_points == 50
        assert relocked.reward_locked_at == _DEADLINE + timedelta(hours=7)
        assert relocked.status == ClaimStatus.UNDER_REVIEW.value
        assert relocked.latest_submission_id == v2.id

        history = asyncio.run(_history_rows(factory, seed.claim.id))
        relock_rows = [
            row
            for row in history
            if row.lock_status_from == RewardLockStatus.INVALIDATED.value
            and row.lock_status_to == RewardLockStatus.PROVISIONAL.value
        ]
        assert len(relock_rows) == 1
        assert relock_rows[0].reward_tier_locked == 50
        assert relock_rows[0].locked_reward_points == 50
        assert len(lock_collector.of_type(REWARD_LOCKED)) == 1
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, run=run))


@pytest.mark.integration
def test_relock_in_revision_window_past_grace_clamps_to_lowest_tier() -> None:
    """The controller-ruling clamp (spec §11.3 amendment): an INVALIDATED
    lock plus a revision-window submission at grace + 25h re-locks
    PROVISIONAL at the lowest defined tier 20% — the §9.3 ladder's
    closed arm does NOT reject the re-lock and nothing 500s."""
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    try:
        seed = asyncio.run(
            _seed(
                factory,
                run,
                claim_status=ClaimStatus.REVISION_REQUIRED,
                reward_lock_status=RewardLockStatus.INVALIDATED,
                reward_tier_locked=None,
                locked_reward_points=None,
                reward_locked_at=None,
                # The teacher reviewed just before grace and the window
                # runs to grace + 48h, so a grace + 25h submission is legal.
                revision_deadline_at=_GRACE + timedelta(hours=48),
                latest_version=2,
                submissions=(
                    SubmissionSpec(1, _DEADLINE, review_status="REVISION_REQUIRED"),
                    SubmissionSpec(2, _GRACE + timedelta(hours=25)),
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
        # Not RewardWindowInconsistentError, not any 500: the call returns.
        lock_service, lock_collector = _lock_service(_GRACE + timedelta(hours=26))
        relocked = _call(
            factory,
            lambda session: lock_service.on_validation_passed(
                session, seed.submissions[2].id
            ),
        )
        assert isinstance(relocked, AssignmentClaim)
        assert relocked.reward_lock_status == RewardLockStatus.PROVISIONAL.value
        assert relocked.reward_tier_locked == 20  # the clamp, not the ladder's refusal
        assert relocked.locked_reward_points == 20
        assert relocked.reward_locked_at == _GRACE + timedelta(hours=25)
        assert relocked.status == ClaimStatus.UNDER_REVIEW.value
        assert relocked.latest_submission_id == seed.submissions[2].id

        relock_rows = [
            row
            for row in asyncio.run(_history_rows(factory, seed.claim.id))
            if row.lock_status_from == RewardLockStatus.INVALIDATED.value
            and row.lock_status_to == RewardLockStatus.PROVISIONAL.value
        ]
        assert len(relock_rows) == 1
        assert relock_rows[0].reward_tier_locked == 20
        assert relock_rows[0].locked_reward_points == 20
        events = lock_collector.of_type(REWARD_LOCKED)
        assert len(events) == 1
        assert events[0].payload["reward_tier_locked"] == 20
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, run=run))


@pytest.mark.integration
def test_invalidate_rejected_without_provisional_lock() -> None:
    """Only a PROVISIONAL lock can be invalidated: CONFIRMED is final
    (the reward was already granted on it) and NONE means there is
    nothing to cancel — both are typed rejections, nothing written."""
    factory = _new_factory()
    for lock_status, tier, points in (
        (RewardLockStatus.CONFIRMED, 100, 100),
        (RewardLockStatus.NONE, None, None),
    ):
        run = uuid4().hex[:8]
        task_ids: list[UUID] = []
        try:
            seed = asyncio.run(
                _seed(
                    factory,
                    run,
                    reward_lock_status=lock_status,
                    reward_tier_locked=tier,
                    locked_reward_points=points,
                )
            )
            task_ids.append(seed.task.id)
            service, collector, _points = _service(_REVIEWED_AT)

            with pytest.raises(BusinessError) as excinfo:
                _call(
                    factory,
                    lambda session, svc=service, world=seed: svc.invalidate_reward_lock(
                        session,
                        world.actor(world.owner),
                        world.submissions[1].id,
                        " attempted invalidation",
                    ),
                )
            assert isinstance(excinfo.value, LockNotInvalidatableError)
            assert excinfo.value.details["reward_lock_status"] == lock_status.value

            claim, _assignment, submission = asyncio.run(
                _reload(factory, seed.claim.id)
            )
            assert claim.reward_lock_status == lock_status.value
            assert claim.reward_tier_locked == tier
            assert claim.status == ClaimStatus.UNDER_REVIEW.value
            assert submission.review_status == "PENDING_REVIEW"
            assert asyncio.run(_history_rows(factory, seed.claim.id)) == []
            assert collector.events == []
        finally:
            asyncio.run(_cleanup(factory, task_ids=task_ids, run=run))


# --- the §14 approve transaction -------------------------------------------------


@pytest.mark.integration
def test_approve_writes_all_ten_transaction_effects() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    try:
        seed = asyncio.run(_seed(factory, run))
        task_ids.append(seed.task.id)
        service, collector, points = _service(_REVIEWED_AT)

        result = _call(
            factory,
            lambda session: service.approve_submission(
                session, seed.actor(seed.owner), seed.submissions[1].id
            ),
        )
        assert isinstance(result, ApprovalResult)
        assert result.already_reviewed is False
        assert result.grant is not None
        assert result.grant.points_granted == 100

        # Steps 5-6: Submission APPROVED, Claim COMPLETED + terminal_at.
        claim, assignment, submission = asyncio.run(_reload(factory, seed.claim.id))
        assert submission.review_status == "APPROVED"
        assert submission.reviewer_id == seed.owner.id
        assert submission.reviewed_at == _REVIEWED_AT
        assert submission.review_note is None
        assert claim.status == ClaimStatus.COMPLETED.value
        assert claim.terminal_at == _REVIEWED_AT

        # Step 7: reward lock CONFIRMED at the locked values + history.
        assert claim.reward_lock_status == RewardLockStatus.CONFIRMED.value
        assert claim.reward_tier_locked == 100
        assert claim.locked_reward_points == 100
        assert claim.reward_locked_at == _DEADLINE
        history = asyncio.run(_history_rows(factory, seed.claim.id))
        assert len(history) == 1
        assert history[0].lock_status_from == RewardLockStatus.PROVISIONAL.value
        assert history[0].lock_status_to == RewardLockStatus.CONFIRMED.value
        assert history[0].reward_tier_locked == 100
        assert history[0].locked_reward_points == 100
        assert history[0].changed_by == seed.owner.id
        assert history[0].submission_id == seed.submissions[1].id

        # Step 8: the frozen port call — one grant, stable idempotency key.
        assert points.grant_count == 1
        call = points.calls[0]
        assert call.user_id == seed.student.id
        assert call.claim_id == seed.claim.id
        assert call.base_points == 100
        assert call.locked_points == 100
        assert call.idempotency_key == f"assignment_reward:{seed.claim.id}"

        # Step 9: Assignment COMPLETED — permanently unallocatable.
        assert assignment.availability_status == AssignmentAvailability.COMPLETED.value

        # Step 10: the audit event.
        rows = asyncio.run(_review_rows(factory, seed.submissions[1].id))
        assert [row.action for row in rows] == [ReviewAction.APPROVE.value]
        events = collector.of_type(SUBMISSION_APPROVED_EVENT)
        assert len(events) == 1
        assert events[0].aggregate_id == seed.claim.id
        assert events[0].payload["submission_id"] == str(seed.submissions[1].id)
        assert events[0].payload["locked_reward_points"] == 100
        assert events[0].payload["reviewer_id"] == str(seed.owner.id)
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, run=run))


@pytest.mark.integration
def test_concurrent_double_approve_grants_exactly_once() -> None:
    """§14's two-teacher race: both transactions race for the claim row
    (2-party barrier, independent sessions/connections); exactly one
    grants, the second acquires the lock afterwards, sees the terminal
    claim, and returns the idempotent ALREADY_REVIEWED result with no
    second grant, no second history row, no second event."""
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    try:
        seed = asyncio.run(_seed(factory, run))
        task_ids.append(seed.task.id)
        service, collector, points = _service(_REVIEWED_AT)
        actor = seed.actor(seed.owner)

        barrier = asyncio.Barrier(2)

        async def approve_side() -> ApprovalResult:
            async with factory() as session:
                await barrier.wait()
                await asyncio.sleep(random.uniform(0, 0.005))
                return await service.approve_submission(
                    session, actor, seed.submissions[1].id
                )

        async def race() -> tuple[ApprovalResult, ApprovalResult]:
            return await asyncio.gather(approve_side(), approve_side())

        first, second = asyncio.run(race())
        results = (first, second)
        # gather answers in argument order, not arrival order: exactly
        # one side granted, the other got the idempotent result.
        assert sorted(result.already_reviewed for result in results) == [False, True]
        for result in results:
            if result.already_reviewed:
                assert result.grant is None
            else:
                assert result.grant is not None
                assert result.grant.points_granted == 100

        assert points.grant_count == 1
        assert points.calls[0].claim_id == seed.claim.id

        claim, assignment, submission = asyncio.run(_reload(factory, seed.claim.id))
        assert claim.status == ClaimStatus.COMPLETED.value
        assert claim.terminal_at == _REVIEWED_AT
        assert claim.reward_lock_status == RewardLockStatus.CONFIRMED.value
        assert submission.review_status == "APPROVED"
        assert assignment.availability_status == AssignmentAvailability.COMPLETED.value
        history = asyncio.run(_history_rows(factory, seed.claim.id))
        assert len(history) == 1  # one confirmation, never two
        assert len(asyncio.run(_review_rows(factory, seed.submissions[1].id))) == 1
        assert len(collector.of_type(SUBMISSION_APPROVED_EVENT)) == 1
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, run=run))


@pytest.mark.integration
def test_approve_on_completed_claim_is_idempotent_already_reviewed() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    try:
        seed = asyncio.run(
            _seed(
                factory,
                run,
                claim_status=ClaimStatus.COMPLETED,
                reward_lock_status=RewardLockStatus.CONFIRMED,
                submissions=(SubmissionSpec(1, _DEADLINE, review_status="APPROVED"),),
            )
        )
        task_ids.append(seed.task.id)
        service, collector, points = _service(_REVIEWED_AT)

        result = _call(
            factory,
            lambda session: service.approve_submission(
                session, seed.actor(seed.owner), seed.submissions[1].id
            ),
        )
        assert isinstance(result, ApprovalResult)
        assert result.already_reviewed is True
        assert result.grant is None
        assert points.grant_count == 0
        claim, _assignment, _submission = asyncio.run(_reload(factory, seed.claim.id))
        assert claim.status == ClaimStatus.COMPLETED.value
        assert claim.terminal_at == _GRACE
        assert asyncio.run(_history_rows(factory, seed.claim.id)) == []
        assert asyncio.run(_review_rows(factory, seed.submissions[1].id)) == []
        assert collector.events == []
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, run=run))


@pytest.mark.integration
def test_approve_requires_a_provisional_lock() -> None:
    """Step 7 needs values to confirm: an approve reaching a claim whose
    lock is not PROVISIONAL (the on_validation_passed race, or an
    invalidated lock awaiting resubmission) is a typed rejection, never
    a CONFIRMED lock over empty values."""
    factory = _new_factory()
    for lock_status, tier, points in (
        (RewardLockStatus.NONE, None, None),
        (RewardLockStatus.INVALIDATED, None, None),
    ):
        run = uuid4().hex[:8]
        task_ids: list[UUID] = []
        try:
            seed = asyncio.run(
                _seed(
                    factory,
                    run,
                    reward_lock_status=lock_status,
                    reward_tier_locked=tier,
                    locked_reward_points=points,
                    reward_locked_at=None,
                )
            )
            task_ids.append(seed.task.id)
            service, _collector, fake = _service(_REVIEWED_AT)

            with pytest.raises(BusinessError) as excinfo:
                _call(
                    factory,
                    lambda session, svc=service, world=seed: svc.approve_submission(
                        session,
                        world.actor(world.owner),
                        world.submissions[1].id,
                    ),
                )
            assert isinstance(excinfo.value, LockNotProvisionalError)
            assert excinfo.value.details["reward_lock_status"] == lock_status.value
            assert fake.grant_count == 0
            claim, assignment, _submission = asyncio.run(
                _reload(factory, seed.claim.id)
            )
            assert claim.status == ClaimStatus.UNDER_REVIEW.value
            assert claim.terminal_at is None
            assert (
                assignment.availability_status == AssignmentAvailability.OCCUPIED.value
            )
        finally:
            asyncio.run(_cleanup(factory, task_ids=task_ids, run=run))


# --- the gates: permission, staleness, state -------------------------------------


@pytest.mark.integration
def test_approve_denies_unauthorized_reviewer_before_lock_state() -> None:
    """Gate order (final-review M1): the reviewer permission check runs
    BEFORE the LockNotProvisional gate — an unauthorized teacher on a
    non-provisional claim learns PERMISSION_DENIED, never the claim's
    lock state (the invalidate path's permission-first ordering)."""
    factory = _new_factory()
    for lock_status in (RewardLockStatus.NONE, RewardLockStatus.INVALIDATED):
        run = uuid4().hex[:8]
        task_ids: list[UUID] = []
        try:
            seed = asyncio.run(
                _seed(
                    factory,
                    run,
                    reward_lock_status=lock_status,
                    reward_tier_locked=None,
                    locked_reward_points=None,
                    reward_locked_at=None,
                )
            )
            task_ids.append(seed.task.id)
            service, _collector, points = _service(_REVIEWED_AT)

            with pytest.raises(BusinessError) as excinfo:
                _call(
                    factory,
                    lambda session, svc=service, world=seed: svc.approve_submission(
                        session,
                        world.actor(world.teacher_other),
                        world.submissions[1].id,
                    ),
                )
            assert isinstance(excinfo.value, ReviewerPermissionDeniedError)
            assert excinfo.value.code is ErrorCode.PERMISSION_DENIED
            assert points.grant_count == 0
            claim, _assignment, submission = asyncio.run(
                _reload(factory, seed.claim.id)
            )
            assert claim.status == ClaimStatus.UNDER_REVIEW.value
            assert claim.terminal_at is None
            assert submission.review_status == "PENDING_REVIEW"
            assert asyncio.run(_review_rows(factory, seed.submissions[1].id)) == []
        finally:
            asyncio.run(_cleanup(factory, task_ids=task_ids, run=run))


@pytest.mark.integration
def test_approve_on_sticky_assignment_status_still_completes_claim() -> None:
    """Assignment stickiness (final-review M2): a RETIRED or
    pre-COMPLETED assignment is terminal (§8.2) — the approve no-ops the
    availability flip instead of erroring, and the claim still
    completes with the lock confirmed and exactly one grant."""
    factory = _new_factory()
    for sticky in (AssignmentAvailability.COMPLETED, AssignmentAvailability.RETIRED):
        run = uuid4().hex[:8]
        task_ids: list[UUID] = []
        try:
            seed = asyncio.run(_seed(factory, run, assignment_status=sticky))
            task_ids.append(seed.task.id)
            service, _collector, points = _service(_REVIEWED_AT)

            result = _call(
                factory,
                lambda session, svc=service, world=seed: svc.approve_submission(
                    session, world.actor(world.owner), world.submissions[1].id
                ),
            )
            assert isinstance(result, ApprovalResult)
            assert result.already_reviewed is False
            assert result.grant is not None
            assert result.grant.points_granted == 100

            claim, assignment, submission = asyncio.run(_reload(factory, seed.claim.id))
            assert claim.status == ClaimStatus.COMPLETED.value
            assert claim.terminal_at == _REVIEWED_AT
            assert claim.reward_lock_status == RewardLockStatus.CONFIRMED.value
            assert submission.review_status == "APPROVED"
            # The sticky terminal assignment state is untouched.
            assert assignment.availability_status == sticky.value
            assert points.grant_count == 1
        finally:
            asyncio.run(_cleanup(factory, task_ids=task_ids, run=run))


@pytest.mark.integration
def test_review_permission_matrix() -> None:
    """Owner, Admin, and a REVIEW_SUBMISSIONS collaborator may review; a
    VIEW_TASK-only collaborator, an unrelated teacher, and the student
    may not (spec §4.2/§4.3). Fresh world per actor so each decision
    starts from the same UNDER_REVIEW state."""
    factory = _new_factory()
    matrix = (
        ("owner", "owner", True),
        ("admin", "admin", True),
        ("review", "teacher_review", True),
        ("view", "teacher_view", False),
        ("other", "teacher_other", False),
        ("student", "student", False),
    )
    for _label, field, allowed in matrix:
        run = uuid4().hex[:8]
        seed = asyncio.run(_seed(factory, run))
        task_ids = [seed.task.id]
        try:
            service, _collector, _points = _service(_REVIEWED_AT)
            user = getattr(seed, field)
            actor = seed.actor(user)
            if allowed:
                claim = _call(
                    factory,
                    lambda session, svc=service, who=actor, world=seed: (
                        svc.require_revision(
                            session, who, world.submissions[1].id, "请修改。"
                        )
                    ),
                )
                assert isinstance(claim, AssignmentClaim)
                assert claim.status == ClaimStatus.REVISION_REQUIRED.value
            else:
                with pytest.raises(BusinessError) as excinfo:
                    _call(
                        factory,
                        lambda session, svc=service, who=actor, world=seed: (
                            svc.require_revision(
                                session, who, world.submissions[1].id, "请修改。"
                            )
                        ),
                    )
                assert isinstance(excinfo.value, ReviewerPermissionDeniedError)
                assert excinfo.value.code is ErrorCode.PERMISSION_DENIED
                reloaded, _assignment, _submission = asyncio.run(
                    _reload(factory, seed.claim.id)
                )
                assert reloaded.status == ClaimStatus.UNDER_REVIEW.value
                assert asyncio.run(_review_rows(factory, seed.submissions[1].id)) == []
        finally:
            asyncio.run(_cleanup(factory, task_ids=task_ids, run=run))


@pytest.mark.integration
def test_stale_submission_version_is_rejected() -> None:
    """Only the claim's latest submission can be reviewed: after the
    student resubmitted, acting on the older VALIDATED version is a
    typed stale-version rejection for every review action."""
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    try:
        seed = asyncio.run(
            _seed(
                factory,
                run,
                latest_version=2,
                submissions=(
                    SubmissionSpec(1, _DEADLINE),
                    SubmissionSpec(2, _DEADLINE + timedelta(hours=1)),
                ),
            )
        )
        task_ids.append(seed.task.id)
        owner = seed.actor(seed.owner)

        service_rev, _c1, _p1 = _service(_REVIEWED_AT)
        with pytest.raises(BusinessError) as excinfo:
            _call(
                factory,
                lambda session: service_rev.require_revision(
                    session, owner, seed.submissions[1].id, "退回旧版。"
                ),
            )
        assert isinstance(excinfo.value, StaleSubmissionVersionError)
        assert excinfo.value.details["latest_submission_id"] == str(
            seed.submissions[2].id
        )

        service_app, _c2, points = _service(_REVIEWED_AT)
        with pytest.raises(BusinessError) as excinfo:
            _call(
                factory,
                lambda session: service_app.approve_submission(
                    session, owner, seed.submissions[1].id
                ),
            )
        assert isinstance(excinfo.value, StaleSubmissionVersionError)
        assert points.grant_count == 0

        claim, assignment, submission = asyncio.run(_reload(factory, seed.claim.id))
        assert claim.status == ClaimStatus.UNDER_REVIEW.value
        assert claim.latest_submission_id == seed.submissions[2].id
        assert submission.review_status == "PENDING_REVIEW"
        assert assignment.availability_status == AssignmentAvailability.OCCUPIED.value
        assert asyncio.run(_review_rows(factory, seed.submissions[1].id)) == []
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, run=run))


@pytest.mark.integration
def test_review_actions_reject_terminal_claims_and_unvalidated_submissions() -> None:
    factory = _new_factory()
    # EXPIRED: the expiry side won; no review action may resurrect it.
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    try:
        seed = asyncio.run(_seed(factory, run, claim_status=ClaimStatus.EXPIRED))
        task_ids.append(seed.task.id)
        owner = seed.actor(seed.owner)
        for action in (
            lambda service, session: service.require_revision(
                session, owner, seed.submissions[1].id, "过期后退回。"
            ),
            lambda service, session: service.invalidate_reward_lock(
                session, owner, seed.submissions[1].id, "过期后判无效。"
            ),
            lambda service, session: service.approve_submission(
                session, owner, seed.submissions[1].id
            ),
        ):
            service, _collector, _points = _service(_REVIEWED_AT)
            with pytest.raises(BusinessError) as excinfo:
                _call(factory, lambda session, a=action, svc=service: a(svc, session))
            assert isinstance(excinfo.value, ClaimNotReviewableError)
        claim, _assignment, _submission = asyncio.run(_reload(factory, seed.claim.id))
        assert claim.status == ClaimStatus.EXPIRED.value
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, run=run))

    # Not machine-VALIDATED: the human stage is unreachable (§11.1).
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids = []
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
        service, _collector, _points = _service(_REVIEWED_AT)
        with pytest.raises(BusinessError) as excinfo:
            _call(
                factory,
                lambda session: service.require_revision(
                    session, seed.actor(seed.owner), seed.submissions[1].id, "未校验。"
                ),
            )
        assert isinstance(excinfo.value, SubmissionNotValidatedError)
        claim, _assignment, _submission = asyncio.run(_reload(factory, seed.claim.id))
        assert claim.status == ClaimStatus.UNDER_REVIEW.value
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, run=run))
