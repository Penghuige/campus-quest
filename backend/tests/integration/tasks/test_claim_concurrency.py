# backend/tests/integration/tasks/test_claim_concurrency.py
"""Random-claim concurrency against real PostgreSQL (spec §8.2-8.4; plan 03
task 6; backend-engineering §7: independent sessions and connections).

Scenarios (the brief, verbatim semantics):

- **50-for-10:** 10 AVAILABLE assignments, 50 ACTIVE students, 50
  concurrent claims -> exactly 10 successes on 10 distinct assignments,
  40 ``NO_ASSIGNMENT_AVAILABLE`` (4xx), no unexpected exception ever
  escapes the service (the "no 500" rule), and the database holds exactly
  10 active claims afterwards.
- **Same user, same task, two concurrent claims:** at most one succeeds;
  the loser sees ``TASK_ACTIVE_CLAIM_EXISTS``.
- **Quota 3 under concurrency:** a user holding 2 CLAIMED claims fires 2
  concurrent claims on different tasks -> exactly one succeeds; the loser
  sees ``ASSIGNMENT_LIMIT_REACHED``.
- **Abandonment exclusion (spec §8.2):** an assignment this user
  ABANDONED or EXPIRED is never handed back to them, even though the row
  returned to AVAILABLE.
- **Snapshot fidelity (spec §6.2):** editing the Task's reward, FIXED
  deadline, and schema version after the claim leaves the claim row
  untouched.
- **Eligibility codes (spec §8.4):** every listed failure mode raises its
  own business code with a 4xx status — never a 500.

Harness notes:

- Claims run through ``ClaimService`` with one *independent* session per
  call, all released simultaneously by an ``asyncio.Event`` barrier, so
  the transactions genuinely contend for the same rows (the engine pool
  bounds how many overlap at once; row-level contention, not connection
  count, is what these tests must exercise).
- Seeding and cleanup use their own sessions with REAL commits: the
  savepoint-wrapped ``db_session`` fixture is invisible to other
  connections, so every committed row is removed by explicit committed
  DELETEs in ``finally`` (claims -> assignments -> tasks -> users, the FK
  order).
- Usernames embed a per-run token, so rows leaked by an aborted run can
  never collide with a later seeding pass.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.clock import FrozenClock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.tasks.claim_service import (
    MAX_ACTIVE_CLAIMS,
    REWARD_POLICY_SNAPSHOT_V1,
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
from app.modules.tasks.models import (
    ACTIVE_CLAIM_STATUSES,
    Assignment,
    AssignmentClaim,
    Task,
)

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
    """A claim row from "before this test" (quota seeds, abandonment
    history), carrying plausible snapshot values."""
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
        reward_policy_snapshot=dict(REWARD_POLICY_SNAPSHOT_V1),
        base_reward_points_snapshot=100,
        submission_schema_version=1,
        reward_lock_status=RewardLockStatus.NONE,
        terminal_at=(
            claimed_at + timedelta(minutes=30)
            if status
            in (ClaimStatus.ABANDONED, ClaimStatus.COMPLETED, ClaimStatus.EXPIRED)
            else None
        ),
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


# --- concurrency harness ---------------------------------------------------------


@dataclass(slots=True)
class ClaimOutcome:
    """One claim attempt's terminal state: a claim, a business error, or —
    never, if the service is correct — an unexpected exception."""

    claim: AssignmentClaim | None = None
    error: BusinessError | None = None
    unexpected: BaseException | None = None


async def _claim_one(
    service: ClaimService,
    factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    task_id: UUID,
    start: asyncio.Event,
) -> ClaimOutcome:
    async with factory() as session:
        # Warm the pooled connection BEFORE the barrier: asyncpg connection
        # setup (~10 ms of TCP + auth) otherwise dwarfs the claim's
        # count->insert critical section, so the barrier releases into
        # sequential-looking runs that hide the very interleaving under
        # test (verified by mutation: without the warm-up the suite could
        # not detect a removed user lock).
        await session.execute(text("SELECT 1"))
        await start.wait()  # park every transaction on one barrier
        try:
            claim = await service.claim_random_assignment(session, user_id, task_id)
        except BusinessError as exc:
            return ClaimOutcome(error=exc)
        except Exception as exc:  # the "no 500" failure mode
            return ClaimOutcome(unexpected=exc)
        return ClaimOutcome(claim=claim)


async def _run_concurrently(
    service: ClaimService,
    factory: async_sessionmaker[AsyncSession],
    calls: list[tuple[UUID, UUID]],
) -> list[ClaimOutcome]:
    start = asyncio.Event()
    tasks = [
        asyncio.create_task(_claim_one(service, factory, user_id, task_id, start))
        for user_id, task_id in calls
    ]
    await asyncio.sleep(0.05)  # let every coroutine reach the barrier
    start.set()
    return list(await asyncio.gather(*tasks))


def _factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


# --- scenario 1: 50-for-10 -------------------------------------------------------


@pytest.mark.integration
async def test_fifty_students_race_ten_available_assignments(
    db_engine: AsyncEngine,
) -> None:
    """The headline invariant: under 50-way contention exactly the 10
    assignments are handed out once each; everyone else gets the 4xx
    NO_ASSIGNMENT_AVAILABLE and nothing blows up."""
    factory = _factory(db_engine)
    service = ClaimService(clock=FrozenClock(_NOW))
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        async with factory() as session:
            teacher = _user(username=f"t{run}", role=Role.TEACHER)
            students = [_user(username=f"2025{run}{i:03d}") for i in range(50)]
            await _persist(session, teacher, *students)
            task = _task(teacher)
            await _persist(session, task)
            assignments = [
                _assignment(task, keyword=f"考研英语{i:03d}") for i in range(10)
            ]
            await _persist(session, *assignments)
            await session.commit()
            task_ids.append(task.id)
            user_ids.append(teacher.id)
            user_ids.extend(student.id for student in students)

        outcomes = await _run_concurrently(
            service, factory, [(user_id, task_ids[0]) for user_id in user_ids[1:]]
        )

        successes = [o for o in outcomes if o.claim is not None]
        failures = [o for o in outcomes if o.error is not None]
        blown_up = [o for o in outcomes if o.unexpected is not None]

        assert not blown_up, [repr(o.unexpected) for o in blown_up]
        assert len(successes) == 10
        assert len({o.claim.assignment_id for o in successes}) == 10
        assert len(failures) == 40
        assert {o.error.code for o in failures} == {ErrorCode.NO_ASSIGNMENT_AVAILABLE}
        assert all(o.error.status_code < 500 for o in failures)

        async with factory() as session:
            active_claims = (
                await session.scalars(
                    select(AssignmentClaim)
                    .where(
                        AssignmentClaim.task_id == task_ids[0],
                        AssignmentClaim.status.in_(ACTIVE_CLAIM_STATUSES),
                    )
                    .order_by(AssignmentClaim.claimed_at)
                )
            ).all()
            assert len(active_claims) == 10
            for claim in active_claims:
                assert claim.status == ClaimStatus.CLAIMED
                assert claim.claimed_at == _NOW
                assert claim.base_reward_points_snapshot == 100
                # RELATIVE task: deadlines anchor on claimed_at (spec §9.2).
                assert claim.deadline_at == _NOW + timedelta(minutes=4320)
                assert claim.grace_deadline_at == claim.deadline_at + timedelta(
                    minutes=1440
                )
                assert claim.submission_schema_version == 2
                assert claim.reward_policy_snapshot == REWARD_POLICY_SNAPSHOT_V1
                assert claim.reward_lock_status == RewardLockStatus.NONE

            occupied = (
                await session.scalars(
                    select(Assignment).where(
                        Assignment.task_id == task_ids[0],
                        Assignment.availability_status
                        == AssignmentAvailability.OCCUPIED,
                    )
                )
            ).all()
            available = (
                await session.scalars(
                    select(Assignment).where(
                        Assignment.task_id == task_ids[0],
                        Assignment.availability_status
                        == AssignmentAvailability.AVAILABLE,
                    )
                )
            ).all()
            assert len(occupied) == 10
            assert len(available) == 0
            assert {assignment.id for assignment in occupied} == {
                o.claim.assignment_id for o in successes
            }
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- scenario 2: same user, same task --------------------------------------------


@pytest.mark.integration
async def test_same_user_concurrent_claims_same_task_at_most_one(
    db_engine: AsyncEngine,
) -> None:
    """Two simultaneous claims by one user on one task: the user-level
    serialization lock lets exactly one through; the other observes the
    non-terminal claim and gets TASK_ACTIVE_CLAIM_EXISTS (4xx)."""
    factory = _factory(db_engine)
    service = ClaimService(clock=FrozenClock(_NOW))
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        async with factory() as session:
            teacher = _user(username=f"t{run}", role=Role.TEACHER)
            student = _user(username=f"2025{run}001")
            await _persist(session, teacher, student)
            task = _task(teacher)
            await _persist(session, task)
            assignments = [
                _assignment(task, keyword=f"考研政治{i:03d}") for i in range(5)
            ]
            await _persist(session, *assignments)
            await session.commit()
            task_ids.append(task.id)
            user_ids.extend([teacher.id, student.id])

        outcomes = await _run_concurrently(
            service, factory, [(student.id, task_ids[0])] * 2
        )

        successes = [o for o in outcomes if o.claim is not None]
        failures = [o for o in outcomes if o.error is not None]
        assert not [o for o in outcomes if o.unexpected is not None]
        assert len(outcomes) == 2
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0].error.code == ErrorCode.TASK_ACTIVE_CLAIM_EXISTS
        assert failures[0].error.status_code < 500

        async with factory() as session:
            active_claims = (
                await session.scalars(
                    select(AssignmentClaim).where(
                        AssignmentClaim.task_id == task_ids[0],
                        AssignmentClaim.user_id == student.id,
                        AssignmentClaim.status.in_(ACTIVE_CLAIM_STATUSES),
                    )
                )
            ).all()
            assert len(active_claims) == 1
            occupied = (
                await session.scalars(
                    select(Assignment).where(
                        Assignment.task_id == task_ids[0],
                        Assignment.availability_status
                        == AssignmentAvailability.OCCUPIED,
                    )
                )
            ).all()
            assert len(occupied) == 1
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- scenario 3: global quota of 3 ------------------------------------------------


@pytest.mark.integration
async def test_quota_three_blocks_fourth_concurrent_claim(
    db_engine: AsyncEngine,
) -> None:
    """A user already holding 2 CLAIMED claims fires 2 concurrent claims on
    2 different tasks: the user-row lock serializes them, the first counts
    2 < 3 and wins, the second counts 3 and gets ASSIGNMENT_LIMIT_REACHED
    (spec §8.2: COUNT then INSERT is only safe under the lock)."""
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
            held_tasks = [_task(teacher, title=f"进行中任务{i}") for i in range(2)]
            fresh_tasks = [_task(teacher, title=f"新任务{i}") for i in range(2)]
            await _persist(session, *held_tasks, *fresh_tasks)
            held_assignments = [
                _assignment(task, keyword=f"考研数学{i}")
                for i, task in enumerate(held_tasks)
            ]
            fresh_assignments = [
                _assignment(task, keyword=f"考研逻辑{i}")
                for i, task in enumerate(fresh_tasks)
            ]
            await _persist(session, *held_assignments, *fresh_assignments)
            await _persist(
                session,
                *(
                    _existing_claim(assignment, student, status=ClaimStatus.CLAIMED)
                    for assignment in held_assignments
                ),
            )
            await session.commit()
            task_ids.extend(task.id for task in (*held_tasks, *fresh_tasks))
            user_ids.extend([teacher.id, student.id])

        outcomes = await _run_concurrently(
            service,
            factory,
            [(student.id, task.id) for task in fresh_tasks],
        )

        successes = [o for o in outcomes if o.claim is not None]
        failures = [o for o in outcomes if o.error is not None]
        assert not [o for o in outcomes if o.unexpected is not None]
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0].error.code == ErrorCode.ASSIGNMENT_LIMIT_REACHED
        assert failures[0].error.status_code < 500
        assert failures[0].error.details == {"limit": MAX_ACTIVE_CLAIMS}

        async with factory() as session:
            active_count = len(
                (
                    await session.scalars(
                        select(AssignmentClaim.id).where(
                            AssignmentClaim.user_id == student.id,
                            AssignmentClaim.status.in_(ACTIVE_CLAIM_STATUSES),
                        )
                    )
                ).all()
            )
            assert active_count == 3  # exactly at the quota, never past it
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- scenario 3b: the user-row lock, deterministically ----------------------------


@pytest.mark.integration
async def test_user_row_lock_serializes_quota_check(db_engine: AsyncEngine) -> None:
    """Deterministic companion to the timing-based quota test: while a
    transaction holds the stable user-level resource (spec §8.3: User row
    FOR UPDATE) with an uncommitted claim, a new claim by the same user
    BLOCKS instead of reading a stale quota count, and re-counts after the
    holder commits. With the lock removed, the blocked-assertion below
    fails — the claim sails through on the pre-insert count."""
    factory = _factory(db_engine)
    service = ClaimService(clock=FrozenClock(_NOW))
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    blocked_task: asyncio.Task[AssignmentClaim] | None = None
    try:
        async with factory() as session:
            teacher = _user(username=f"t{run}", role=Role.TEACHER)
            student = _user(username=f"2025{run}007")
            await _persist(session, teacher, student)
            held_tasks = [_task(teacher, title=f"进行中任务{i}") for i in range(2)]
            spare_tasks = [_task(teacher, title=f"并发任务{i}") for i in range(2)]
            await _persist(session, *held_tasks, *spare_tasks)
            held_assignments = [
                _assignment(task, keyword=f"考研数学{i}")
                for i, task in enumerate(held_tasks)
            ]
            spare_assignments = [
                _assignment(task, keyword=f"考研逻辑{i}")
                for i, task in enumerate(spare_tasks)
            ]
            await _persist(session, *held_assignments, *spare_assignments)
            await _persist(
                session,
                *(
                    _existing_claim(assignment, student, status=ClaimStatus.CLAIMED)
                    for assignment in held_assignments
                ),
            )
            await session.commit()
            task_ids.extend(task.id for task in (*held_tasks, *spare_tasks))
            user_ids.extend([teacher.id, student.id])

        # "Session A" plays an in-flight claim transaction: it holds the
        # exact stable resource the service must serialize on.
        async with factory() as holder:
            await holder.execute(
                text("select status from users where id = :uid for update"),
                {"uid": student.id},
            )

            async def blocked_claim() -> AssignmentClaim:
                async with factory() as session:
                    return await service.claim_random_assignment(
                        session, student.id, spare_tasks[1].id
                    )

            blocked_task = asyncio.create_task(blocked_claim())
            done, pending = await asyncio.wait({blocked_task}, timeout=0.3)
            assert blocked_task in pending, (
                "claim completed while another same-user transaction held the "
                "user-row lock: the quota check is not serialized"
            )

            # The holder's claim lands (count 2 -> 3) and commits.
            holder.add(_existing_claim(spare_assignments[0], student))
            await holder.commit()

        with pytest.raises(BusinessError) as exc_info:
            await blocked_task
        assert exc_info.value.code == ErrorCode.ASSIGNMENT_LIMIT_REACHED
        assert exc_info.value.status_code < 500
    finally:
        if blocked_task is not None and not blocked_task.done():
            blocked_task.cancel()
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- scenario 4: abandoned/expired assignments never come back --------------------


@pytest.mark.integration
async def test_abandoned_or_expired_assignment_not_reassigned_to_same_user(
    db_engine: AsyncEngine,
) -> None:
    """The exclusion set (spec §8.2/§8.3): assignments this user ABANDONED
    or EXPIRED are AVAILABLE again but never handed back to them. The
    single-assignment task pins this deterministically — without the
    exclusion its only assignment would come out and the claim succeed."""
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
            task = _task(teacher)
            # A task whose ONLY assignment this user previously abandoned:
            # claiming it must report NO_ASSIGNMENT_AVAILABLE even though
            # the row itself is AVAILABLE again.
            lonely_task = _task(teacher, title="唯一单元曾被放弃的任务")
            await _persist(session, task, lonely_task)
            abandoned = _assignment(task, keyword="考研英语")
            fresh = _assignment(task, keyword="考研政治")
            expired = _assignment(task, keyword="考研数学")
            lonely = _assignment(lonely_task, keyword="考研英语")
            await _persist(session, abandoned, fresh, expired, lonely)
            await _persist(
                session,
                _existing_claim(abandoned, student, status=ClaimStatus.ABANDONED),
                _existing_claim(expired, student, status=ClaimStatus.EXPIRED),
                _existing_claim(lonely, student, status=ClaimStatus.ABANDONED),
            )
            await session.commit()
            task_ids.extend([task.id, lonely_task.id])
            user_ids.extend([teacher.id, student.id])

        # The lonely task deterministically proves the exclusion: its only
        # candidate is the abandoned assignment.
        async with factory() as session:
            with pytest.raises(BusinessError) as exc_info:
                await service.claim_random_assignment(
                    session, student.id, lonely_task.id
                )
        assert exc_info.value.code == ErrorCode.NO_ASSIGNMENT_AVAILABLE
        assert exc_info.value.status_code < 500

        # On the mixed task, both excluded rows are AVAILABLE, yet only
        # `fresh` can come out.
        async with factory() as session:
            claim = await service.claim_random_assignment(session, student.id, task.id)
            assert claim.assignment_id == fresh.id

        # Abandon the fresh one too: now every candidate is excluded and
        # the task reports NO_ASSIGNMENT_AVAILABLE instead of recycling.
        async with factory() as session:
            loaded = await session.get(AssignmentClaim, claim.id)
            assert loaded is not None
            loaded.status = ClaimStatus.ABANDONED
            loaded.terminal_at = _NOW + timedelta(minutes=5)
            fresh_row = await session.get(Assignment, fresh.id)
            assert fresh_row is not None
            fresh_row.availability_status = AssignmentAvailability.AVAILABLE
            await session.commit()

        async with factory() as session:
            with pytest.raises(BusinessError) as exc_info:
                await service.claim_random_assignment(session, student.id, task.id)
        assert exc_info.value.code == ErrorCode.NO_ASSIGNMENT_AVAILABLE
        assert exc_info.value.status_code < 500
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- scenario 5: claim snapshot fidelity ------------------------------------------


@pytest.mark.integration
async def test_claim_snapshot_survives_later_task_edits(
    db_engine: AsyncEngine,
) -> None:
    """Spec §6.2: the five MUST-snapshot fields (plus claimed_at) are
    frozen at claim time; a later Task edit of reward, FIXED deadline, and
    schema version never rewrites an existing claim."""
    factory = _factory(db_engine)
    clock = FrozenClock(_NOW)
    service = ClaimService(clock=clock)
    run = uuid4().hex[:8]
    fixed_deadline = _NOW + timedelta(days=7)

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        async with factory() as session:
            teacher = _user(username=f"t{run}", role=Role.TEACHER)
            student = _user(username=f"2025{run}004")
            await _persist(session, teacher, student)
            task = _task(
                teacher,
                deadline_mode=DeadlineMode.FIXED,
                fixed_deadline_at=fixed_deadline,
                duration_minutes=None,
            )
            await _persist(session, task)
            await _persist(session, _assignment(task, keyword="考研英语"))
            await session.commit()
            task_ids.append(task.id)
            user_ids.extend([teacher.id, student.id])

        async with factory() as session:
            claim = await service.claim_random_assignment(session, student.id, task.id)
            assert claim.claimed_at == _NOW
            assert claim.base_reward_points_snapshot == 100
            assert claim.deadline_at == fixed_deadline
            assert claim.grace_deadline_at == fixed_deadline + timedelta(minutes=1440)
            assert claim.submission_schema_version == 2
            assert claim.reward_policy_snapshot == REWARD_POLICY_SNAPSHOT_V1

        # Teacher-side configuration change AFTER students have claimed.
        async with factory() as session:
            loaded_task = await session.get(Task, task.id)
            assert loaded_task is not None
            loaded_task.base_reward_points = 500
            loaded_task.fixed_deadline_at = fixed_deadline + timedelta(days=3)
            loaded_task.submission_schema_version = 3
            await session.commit()

        async with factory() as session:
            reloaded_task = await session.get(Task, task.id)
            assert reloaded_task is not None
            assert reloaded_task.base_reward_points == 500
            assert reloaded_task.submission_schema_version == 3
            reloaded_claim = await session.get(AssignmentClaim, claim.id)
            assert reloaded_claim is not None
            assert reloaded_claim.base_reward_points_snapshot == 100
            assert reloaded_claim.deadline_at == fixed_deadline
            assert reloaded_claim.grace_deadline_at == fixed_deadline + timedelta(
                minutes=1440
            )
            assert reloaded_claim.submission_schema_version == 2
            assert reloaded_claim.reward_policy_snapshot == REWARD_POLICY_SNAPSHOT_V1
            assert reloaded_claim.claimed_at == _NOW
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- scenario 6: eligibility codes are 4xx, never 500 ------------------------------


@pytest.mark.integration
async def test_eligibility_failures_map_to_business_codes(
    db_engine: AsyncEngine,
) -> None:
    """Spec §8.4: each checklist failure raises its own stable code with a
    4xx status — ACCOUNT_NOT_ACTIVE, TASK_NOT_CLAIMABLE,
    CLAIM_CUTOFF_REACHED, TASK_ACTIVE_CLAIM_EXISTS, and
    NO_ASSIGNMENT_AVAILABLE."""
    factory = _factory(db_engine)
    service = ClaimService(clock=FrozenClock(_NOW))
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        async with factory() as session:
            teacher = _user(username=f"t{run}", role=Role.TEACHER)
            student = _user(username=f"2025{run}005")
            suspended = _user(username=f"2025{run}006", status=UserStatus.SUSPENDED)
            await _persist(session, teacher, student, suspended)
            claimable = _task(teacher, title="正常任务")
            draft = _task(teacher, title="草稿任务", status=TaskStatus.DRAFT)
            near_deadline = _task(
                teacher,
                title="临近截止任务",
                deadline_mode=DeadlineMode.FIXED,
                fixed_deadline_at=_NOW + timedelta(hours=2),  # < 240 min cutoff
                duration_minutes=None,
            )
            taken = _task(teacher, title="已领取任务")
            empty = _task(teacher, title="空任务")
            await _persist(session, claimable, draft, near_deadline, taken, empty)
            taken_seed = _assignment(taken, keyword="关键词F")
            await _persist(
                session,
                _assignment(claimable, keyword="关键词A"),
                _assignment(draft, keyword="关键词B"),
                _assignment(near_deadline, keyword="关键词C"),
                _assignment(taken, keyword="关键词D"),
                _assignment(taken, keyword="关键词E"),
                taken_seed,
            )
            await _persist(session, _existing_claim(taken_seed, student))
            await session.commit()
            task_ids.extend(
                task.id for task in (claimable, draft, near_deadline, taken, empty)
            )
            user_ids.extend([teacher.id, student.id, suspended.id])

        async def _error_for(user_id: UUID, target: Task) -> BusinessError:
            with pytest.raises(BusinessError) as exc_info:
                async with factory() as session:
                    await service.claim_random_assignment(session, user_id, target.id)
            return exc_info.value

        error = await _error_for(suspended.id, claimable)
        assert error.code == ErrorCode.ACCOUNT_NOT_ACTIVE
        assert error.status_code == 403

        error = await _error_for(student.id, draft)
        assert error.code == ErrorCode.TASK_NOT_CLAIMABLE
        assert error.status_code < 500

        error = await _error_for(student.id, near_deadline)
        assert error.code == ErrorCode.CLAIM_CUTOFF_REACHED
        assert error.status_code < 500

        error = await _error_for(student.id, taken)
        assert error.code == ErrorCode.TASK_ACTIVE_CLAIM_EXISTS
        assert error.status_code < 500

        error = await _error_for(student.id, empty)
        assert error.code == ErrorCode.NO_ASSIGNMENT_AVAILABLE
        assert error.status_code < 500
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)
