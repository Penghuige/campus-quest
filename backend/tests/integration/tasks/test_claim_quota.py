# backend/tests/integration/tasks/test_claim_quota.py
"""Claim eligibility end-to-end against real PostgreSQL (spec §8.2, §8.4,
§9.1; plan 03 task 7; backend-engineering §21: service behavior belongs
on the real database).

Every scenario drives the full ``ClaimService.claim_random_assignment``
flow — user-row lock, FOR SHARE task read, ``ClaimEligibilityService``
check inside the locked transaction, candidate selection, snapshot,
commit — and asserts the §8.4 code the checklist must surface:

- **Quota:** a student holding 3 actionable claims (2 CLAIMED + 1
  REVISION_REQUIRED) is refused a 4th with ASSIGNMENT_LIMIT_REACHED and
  the database is left untouched.
- **Slot release:** moving one CLAIMED claim to UNDER_REVIEW (review
  started; §8.2 — the student can no longer act on it) frees the slot;
  the next claim succeeds and refills it, so the attempt after that is
  blocked again.
- **Same task:** a second claim on a task that already has a
  non-terminal claim is TASK_ACTIVE_CLAIM_EXISTS, while the same student
  in the same state successfully claims a different task.
- **FIXED cutoff:** a task 3h59m from its deadline refuses claims with
  CLAIM_CUTOFF_REACHED; a task exactly at the 240-minute cutoff still
  claims (spec §9.1: blocked only when LESS than the cutoff remains) and
  snapshots the FIXED deadlines (§9.1: deadline_at = fixed_deadline_at,
  grace = +24h).
- **Role matrix (spec §4.1):** claiming is a Student capability — a
  direct service call for a TEACHER or ADMIN is refused with
  PERMISSION_DENIED at the locked user-row read itself, so no caller
  can bypass the transport guard and a concurrent role change
  linearizes behind the same quota-serialization lock.

Harness notes: seeding, mutations, and assertions use independent
committed sessions from the engine factory (the savepoint-wrapped
``db_session`` fixture is invisible to the service's own sessions);
every test removes its rows with explicit committed DELETEs in
``finally`` (claims -> assignments -> tasks -> users, the FK order).
Usernames embed a per-run token, so rows leaked by an aborted run can
never collide with a later seeding pass.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.clock import FrozenClock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.tasks.claim_service import (
    MAX_ACTIVE_CLAIMS,
    ClaimService,
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

# Direct-insert password stub (argon2 hash of an unguessable test secret);
# the registration service is deliberately not exercised here.
_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)

_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


# --- seeding helpers -------------------------------------------------------------


def _user(
    *, username: str, role: Role = Role.STUDENT, status: UserStatus = UserStatus.ACTIVE
) -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname=f"同学{username[-4:]}",
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
        "submission_schema": {"columns": [{"name": "note", "type": "string"}]},
        "submission_schema_version": 2,
        "allowed_file_types": ["CSV"],
        "max_file_size_bytes": 200 * 1024 * 1024,
        "notification_channels": ["SMS"],
    }
    fields.update(overrides)
    return Task(**fields)


def _assignment(task: Task, *, keyword: str) -> Assignment:
    return Assignment(
        task_id=task.id,
        platform="xiaohongshu",
        keyword=keyword,
        availability_status=AssignmentAvailability.AVAILABLE,
    )


def _existing_claim(
    assignment: Assignment, user: User, *, status: ClaimStatus = ClaimStatus.CLAIMED
) -> AssignmentClaim:
    """A claim row from "before this test" (quota seeds), carrying
    plausible snapshot values."""
    claimed_at = _NOW - timedelta(hours=1)
    deadline = claimed_at + timedelta(days=3)
    return AssignmentClaim(
        assignment_id=assignment.id,
        task_id=assignment.task_id,
        user_id=user.id,
        status=status,
        claimed_at=claimed_at,
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


async def _cleanup(
    factory: async_sessionmaker[AsyncSession],
    *,
    task_ids: list[UUID],
    user_ids: list[UUID],
) -> None:
    """Explicit committed cleanup in FK order (claims -> assignments ->
    tasks -> users); nothing rolls these rows back for us."""
    async with factory() as session:
        if task_ids:
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


def _factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


# The actionable triple that fills the §8.2 quota: two fresh CLAIMED
# claims plus one REVISION_REQUIRED (revision work still needs student
# action, so it occupies a slot).
_HELD_STATUSES = (
    ClaimStatus.CLAIMED,
    ClaimStatus.CLAIMED,
    ClaimStatus.REVISION_REQUIRED,
)


async def _seed_at_quota(
    factory: async_sessionmaker[AsyncSession],
    *,
    run: str,
    extra_tasks: int,
) -> tuple[User, User, list[Task], list[Task], AssignmentClaim]:
    """One teacher, one ACTIVE student holding the actionable triple on 3
    held tasks, plus ``extra_tasks`` fresh claimable tasks with one
    AVAILABLE assignment each. Returns the teacher, the student, the task
    lists, and the first seeded claim (the CLAIMED one the transition
    test moves to UNDER_REVIEW)."""
    async with factory() as session:
        teacher = _user(username=f"t{run}", role=Role.TEACHER)
        student = _user(username=f"2025{run}001")
        await _persist(session, teacher, student)
        held = [_task(teacher, title=f"进行中任务{i}") for i in range(3)]
        fresh = [_task(teacher, title=f"新任务{i}") for i in range(extra_tasks)]
        await _persist(session, *held, *fresh)
        held_assignments = [
            _assignment(task, keyword=f"考研数学{i}") for i, task in enumerate(held)
        ]
        fresh_assignments = [
            _assignment(task, keyword=f"考研逻辑{i}") for i, task in enumerate(fresh)
        ]
        await _persist(session, *held_assignments, *fresh_assignments)
        claims = [
            _existing_claim(assignment, student, status=status)
            for assignment, status in zip(held_assignments, _HELD_STATUSES, strict=True)
        ]
        await _persist(session, *claims)
        await session.commit()
        return teacher, student, held, fresh, claims[0]


# --- quota: the 4th actionable claim is refused -------------------------------------


@pytest.mark.integration
async def test_student_at_quota_is_refused_a_fourth_actionable_claim(
    db_engine: AsyncEngine,
) -> None:
    """Spec §8.2: at most 3 claims that still need student action; the
    4th surfaces ASSIGNMENT_LIMIT_REACHED (4xx) with the limit in
    details, and neither a claim row nor an occupancy change leaks."""
    factory = _factory(db_engine)
    service = ClaimService(clock=FrozenClock(_NOW))
    run = uuid4().hex[:8]

    teacher, student, held, fresh, _first_claim = await _seed_at_quota(
        factory, run=run, extra_tasks=1
    )
    task_ids = [task.id for task in (*held, *fresh)]
    user_ids = [teacher.id, student.id]
    try:
        async with factory() as session:
            with pytest.raises(BusinessError) as exc_info:
                await service.claim_random_assignment(session, student.id, fresh[0].id)
        assert exc_info.value.code == ErrorCode.ASSIGNMENT_LIMIT_REACHED
        assert exc_info.value.status_code < 500
        assert exc_info.value.details == {"limit": MAX_ACTIVE_CLAIMS}

        async with factory() as session:
            assignment = (
                await session.scalars(
                    select(Assignment).where(Assignment.task_id == fresh[0].id)
                )
            ).one()
            assert assignment.availability_status == AssignmentAvailability.AVAILABLE
            claim_ids = (
                await session.scalars(
                    select(AssignmentClaim.id).where(
                        AssignmentClaim.user_id == student.id
                    )
                )
            ).all()
            assert len(claim_ids) == 3  # exactly the seeded triple
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- quota: CLAIMED -> UNDER_REVIEW releases the slot -------------------------------


@pytest.mark.integration
async def test_transition_to_under_review_frees_the_quota_slot(
    db_engine: AsyncEngine,
) -> None:
    """Spec §8.2: a claim whose file is already submitted and under review
    stops occupying one of the 3 slots (the student cannot influence
    review speed). Moving one CLAIMED claim to UNDER_REVIEW lets the next
    claim through; that claim refills the slot, so the attempt after it
    is blocked again."""
    factory = _factory(db_engine)
    service = ClaimService(clock=FrozenClock(_NOW))
    run = uuid4().hex[:8]

    teacher, student, held, fresh, first_claim = await _seed_at_quota(
        factory, run=run, extra_tasks=2
    )
    task_ids = [task.id for task in (*held, *fresh)]
    user_ids = [teacher.id, student.id]
    try:
        # Sanity: the seeded state really is at quota.
        async with factory() as session:
            with pytest.raises(BusinessError) as blocked:
                await service.claim_random_assignment(session, student.id, fresh[0].id)
        assert blocked.value.code == ErrorCode.ASSIGNMENT_LIMIT_REACHED

        # Review starts on one of the CLAIMED claims.
        async with factory() as session:
            loaded = await session.get(AssignmentClaim, first_claim.id)
            assert loaded is not None
            assert loaded.status == ClaimStatus.CLAIMED
            loaded.status = ClaimStatus.UNDER_REVIEW
            await session.commit()

        # The freed slot lets the student claim again...
        async with factory() as session:
            claim = await service.claim_random_assignment(
                session, student.id, fresh[0].id
            )
            assert claim.status == ClaimStatus.CLAIMED
            assert claim.user_id == student.id

        # ...and the new claim refilled it: one more actionable triple.
        async with factory() as session:
            with pytest.raises(BusinessError) as blocked_again:
                await service.claim_random_assignment(session, student.id, fresh[1].id)
        assert blocked_again.value.code == ErrorCode.ASSIGNMENT_LIMIT_REACHED
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- same task: non-terminal conflict vs a different task ---------------------------


@pytest.mark.integration
async def test_second_claim_on_same_task_rejected_other_task_succeeds(
    db_engine: AsyncEngine,
) -> None:
    """Spec §8.2: one non-terminal claim per user per task — the second
    claim on the same task gets TASK_ACTIVE_CLAIM_EXISTS, while the same
    student in the same state successfully claims a different task."""
    factory = _factory(db_engine)
    service = ClaimService(clock=FrozenClock(_NOW))
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        async with factory() as session:
            teacher = _user(username=f"t{run}", role=Role.TEACHER)
            student = _user(username=f"2025{run}002")
            await _persist(session, teacher, student)
            taken = _task(teacher, title="已领取任务")
            other = _task(teacher, title="另一任务")
            await _persist(session, taken, other)
            taken_seed = _assignment(taken, keyword="关键词A")
            taken_spare = _assignment(taken, keyword="关键词B")
            other_assignment = _assignment(other, keyword="关键词C")
            await _persist(session, taken_seed, taken_spare, other_assignment)
            await _persist(session, _existing_claim(taken_seed, student))
            await session.commit()
            task_ids.extend([taken.id, other.id])
            user_ids.extend([teacher.id, student.id])

        # Same task: a second claim must not even consider the spare
        # AVAILABLE assignment — the non-terminal conflict fires first.
        async with factory() as session:
            with pytest.raises(BusinessError) as exc_info:
                await service.claim_random_assignment(session, student.id, taken.id)
        assert exc_info.value.code == ErrorCode.TASK_ACTIVE_CLAIM_EXISTS
        assert exc_info.value.status_code < 500
        assert exc_info.value.details == {"task_id": str(taken.id)}

        # Different task: the same student, same state, succeeds.
        async with factory() as session:
            claim = await service.claim_random_assignment(session, student.id, other.id)
            assert claim.task_id == other.id
            assert claim.status == ClaimStatus.CLAIMED

        async with factory() as session:
            spare = await session.get(Assignment, taken_spare.id)
            assert spare is not None
            assert spare.availability_status == AssignmentAvailability.AVAILABLE
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- FIXED cutoff: past cutoff refuses, exactly-at-cutoff still claims --------------


@pytest.mark.integration
async def test_fixed_task_past_cutoff_refuses_claims(db_engine: AsyncEngine) -> None:
    """Spec §9.1: with less than claim_cutoff_minutes (default 240m) of
    FIXED time left, new claims stop with CLAIM_CUTOFF_REACHED and
    nothing is written."""
    factory = _factory(db_engine)
    service = ClaimService(clock=FrozenClock(_NOW))
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        async with factory() as session:
            teacher = _user(username=f"t{run}", role=Role.TEACHER)
            student = _user(username=f"2025{run}003")
            await _persist(session, teacher, student)
            near = _task(
                teacher,
                title="临近截止任务",
                deadline_mode=DeadlineMode.FIXED,
                fixed_deadline_at=_NOW + timedelta(hours=3, minutes=59),
                duration_minutes=None,
            )
            await _persist(session, near)
            assignment = _assignment(near, keyword="考研英语")
            await _persist(session, assignment)
            await session.commit()
            task_ids.append(near.id)
            user_ids.extend([teacher.id, student.id])

        async with factory() as session:
            with pytest.raises(BusinessError) as exc_info:
                await service.claim_random_assignment(session, student.id, near.id)
        assert exc_info.value.code == ErrorCode.CLAIM_CUTOFF_REACHED
        assert exc_info.value.status_code < 500
        assert exc_info.value.details["claim_cutoff_minutes"] == 240

        async with factory() as session:
            loaded = await session.get(Assignment, assignment.id)
            assert loaded is not None
            assert loaded.availability_status == AssignmentAvailability.AVAILABLE
            claim_ids = (
                await session.scalars(
                    select(AssignmentClaim.id).where(AssignmentClaim.task_id == near.id)
                )
            ).all()
            assert claim_ids == []
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


@pytest.mark.integration
async def test_fixed_task_exactly_at_cutoff_still_claims(
    db_engine: AsyncEngine,
) -> None:
    """The §9.1 boundary end-to-end: "距 deadline 少于 4 小时时停止新领
    取" blocks only when LESS than the cutoff remains, so a task exactly
    240 minutes from its FIXED deadline is still claimable and snapshots
    the FIXED deadlines (deadline_at = fixed_deadline_at, grace +24h)."""
    factory = _factory(db_engine)
    service = ClaimService(clock=FrozenClock(_NOW))
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        async with factory() as session:
            teacher = _user(username=f"t{run}", role=Role.TEACHER)
            student = _user(username=f"2025{run}004")
            await _persist(session, teacher, student)
            fixed_deadline = _NOW + timedelta(minutes=240)  # exactly the cutoff
            boundary = _task(
                teacher,
                title="恰好到达cutoff的任务",
                deadline_mode=DeadlineMode.FIXED,
                fixed_deadline_at=fixed_deadline,
                duration_minutes=None,
            )
            await _persist(session, boundary)
            assignment = _assignment(boundary, keyword="考研政治")
            await _persist(session, assignment)
            await session.commit()
            task_ids.append(boundary.id)
            user_ids.extend([teacher.id, student.id])

        async with factory() as session:
            claim = await service.claim_random_assignment(
                session, student.id, boundary.id
            )
            assert claim.assignment_id == assignment.id
            assert claim.status == ClaimStatus.CLAIMED
            assert claim.claimed_at == _NOW
            assert claim.deadline_at == fixed_deadline
            assert claim.grace_deadline_at == fixed_deadline + timedelta(minutes=1440)
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- role matrix: claiming is a Student capability (spec §4.1) ----------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    "role",
    [
        pytest.param(Role.TEACHER, id="teacher"),
        pytest.param(Role.ADMIN, id="admin"),
    ],
)
async def test_non_student_roles_are_refused_by_the_service_lock(
    db_engine: AsyncEngine, role: Role
) -> None:
    """Spec §4.1: the domain invariant holds without the HTTP route. The
    locked user-row read — the same FOR UPDATE that serializes the quota
    — selects and judges BOTH status and role, so a non-STUDENT caller is
    refused with PERMISSION_DENIED while holding the lock and nothing is
    written (no claim row, no occupancy change)."""
    factory = _factory(db_engine)
    service = ClaimService(clock=FrozenClock(_NOW))
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        async with factory() as session:
            owner = _user(username=f"t{run}", role=Role.TEACHER)
            staff = _user(username=f"staff{run}", role=role)
            await _persist(session, owner, staff)
            task = _task(owner, title="教师账号尝试领取的任务")
            await _persist(session, task)
            assignment = _assignment(task, keyword="考研逻辑")
            await _persist(session, assignment)
            await session.commit()
            task_ids.append(task.id)
            user_ids.extend([owner.id, staff.id])

        async with factory() as session:
            with pytest.raises(BusinessError) as exc_info:
                await service.claim_random_assignment(session, staff.id, task.id)
        assert exc_info.value.code == ErrorCode.PERMISSION_DENIED
        assert exc_info.value.status_code == 403
        assert exc_info.value.details["role"] == role.value

        async with factory() as session:
            loaded = await session.get(Assignment, assignment.id)
            assert loaded is not None
            assert loaded.availability_status == AssignmentAvailability.AVAILABLE
            claim_ids = (
                await session.scalars(
                    select(AssignmentClaim.id).where(AssignmentClaim.task_id == task.id)
                )
            ).all()
            assert claim_ids == []
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)
