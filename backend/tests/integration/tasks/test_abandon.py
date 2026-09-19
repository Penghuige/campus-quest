# backend/tests/integration/tasks/test_abandon.py
"""Claim abandon/release end-to-end against real PostgreSQL (spec §8.5,
§8.2, §0, §38.9, §44.14; plan 03 task 8; backend-engineering §11: business
periods derive from the Clock's UTC instant + BUSINESS_TIMEZONE, never a
hardcoded offset).

Every scenario drives the full ``AbandonService.abandon_claim`` flow —
user-row lock FIRST (the same stable resource T6's claim flow serializes
on, so one user's claims and abandons share one queue), claim-row lock,
natural-day count under that lock, Assignment release, one commit — and
asserts:

- **Daily cap:** the 1st and 2nd abandon of one BUSINESS_TIMEZONE
  natural day succeed; the 3rd is ABANDON_LIMIT_REACHED (4xx, limit in
  details) and writes nothing.
- **Natural-day window:** the counter resets at BUSINESS_TIMEZONE
  midnight, not UTC midnight and not a rolling 24h. The Shanghai case
  puts two abandons on one business day across two different UTC dates
  (17:00Z Jan 15 = Jan 16 01:00 +08 and 06:00Z Jan 16) and only allows
  the next one after 16:00Z (local midnight). The America/New_York case
  (spec §38.9's DST requirement) uses the 23-hour day of the 2026-03-08
  spring-forward: an abandon at 03:30Z Mar 9 is still local Mar 8 and
  still counted, while 04:00Z (local midnight, only 30 minutes later)
  starts a fresh day — a fixed 24h window would fail both assertions.
- **Concurrency:** two concurrent abandons of the SAME claim land as one
  transition plus one idempotent replay (count +1, one event, consistent
  state); two concurrent abandons of DIFFERENT claims with one daily
  slot left let exactly one through — the user-row lock serializes the
  count, so the limit cannot be bypassed (spec §8.5).
- **Reallocation:** after A abandons Assignment X, X is AVAILABLE, B
  receives it, and A can never randomly receive X again in this task's
  lifecycle (the T6 exclusion subquery over ABANDONED claims).
- **Idempotent replay:** re-abandoning an ABANDONED claim returns the
  same terminal result without a second count or a second event.
- **Guards:** VALIDATING/UNDER_REVIEW (and other terminal) claims are
  not a student abandon action (CLAIM_NOT_ABANDONABLE); a foreign or
  missing claim is 403/404; a non-ACTIVE account is refused.
- **No points side effects:** abandon touches only the claim's terminal
  state and the assignment's availability — reward-lock fields stay
  untouched and exactly one CLAIM_ABANDONED audit event is emitted.

Harness notes: seeding, mutations, and assertions use independent
committed sessions from the engine factory (the savepoint-wrapped
``db_session`` fixture is invisible to the service's own sessions);
every test removes its rows with explicit committed DELETEs in
``finally`` (claims -> assignments -> tasks -> users, the FK order).
Usernames embed a per-run token, so rows leaked by an aborted run can
never collide with a later seeding pass.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.clock import FrozenClock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import InMemoryEventCollector
from app.modules.identity.models import User
from app.modules.tasks.abandon_service import (
    CLAIM_ABANDONED,
    DAILY_ABANDON_LIMIT,
    AbandonService,
)
from app.modules.tasks.claim_service import ClaimService
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

# The default BUSINESS_TIMEZONE fixture: 2026-01-15 17:00Z is business day
# 2026-01-16 01:00 in Asia/Shanghai, so UTC date != business date here.
_NOW = datetime(2026, 1, 15, 17, 0, tzinfo=UTC)

# America/New_York around the 2026-03-08 spring-forward: local Mar 8 runs
# from 05:00Z to 04:00Z next day — a 23-hour natural day (spec §38.9).
_NY_DAY_START = datetime(2026, 3, 8, 6, 0, tzinfo=UTC)  # local Mar 8, 01:00 EST


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


def _claim(
    assignment: Assignment,
    user: User,
    *,
    status: ClaimStatus,
    claimed_at: datetime,
    terminal_at: datetime | None = None,
) -> AssignmentClaim:
    """A claim row from "before this test", carrying plausible snapshot
    values. Callers pass ``terminal_at`` only for terminal seeds."""
    deadline = claimed_at + timedelta(days=3)
    return AssignmentClaim(
        assignment_id=assignment.id,
        task_id=assignment.task_id,
        user_id=user.id,
        status=status,
        claimed_at=claimed_at,
        deadline_at=deadline,
        grace_deadline_at=deadline + timedelta(minutes=1440),
        reward_policy_snapshot={"version": 1, "ladder_fractions": ["1", "0.8"]},
        base_reward_points_snapshot=100,
        submission_schema_version=1,
        reward_lock_status=RewardLockStatus.NONE,
        terminal_at=terminal_at,
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


@dataclass(slots=True)
class Seeded:
    """The rows a test seeded: one teacher, one ACTIVE student, and one
    task + assignment + claim per requested status (active statuses hold
    their assignment OCCUPIED; an ABANDONED seed releases it and carries
    a terminal_at one hour before the anchor instant)."""

    teacher: User
    student: User
    tasks: list[Task]
    assignments: list[Assignment]
    claims: list[AssignmentClaim]


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    *,
    run: str,
    statuses: list[ClaimStatus],
    anchor: datetime,
) -> Seeded:
    async with factory() as session:
        teacher = _user(username=f"t{run}", role=Role.TEACHER)
        student = _user(username=f"2025{run}001")
        await _persist(session, teacher, student)
        tasks = [_task(teacher, title=f"任务{i}") for i in range(len(statuses))]
        await _persist(session, *tasks)
        assignments = [
            _assignment(task, keyword=f"考研数学{i}")
            for i, task in enumerate(tasks)
        ]
        await _persist(session, *assignments)
        claims = [
            _claim(
                assignment,
                student,
                status=status,
                claimed_at=anchor - timedelta(hours=2),
                terminal_at=(
                    anchor - timedelta(hours=1)
                    if status is ClaimStatus.ABANDONED
                    else None
                ),
            )
            for assignment, status in zip(assignments, statuses, strict=True)
        ]
        for assignment, status in zip(assignments, statuses, strict=True):
            if status not in (ClaimStatus.ABANDONED, ClaimStatus.EXPIRED):
                # Active/other-terminal seeds occupy their unit exactly as
                # the claim service would have left it.
                assignment.availability_status = AssignmentAvailability.OCCUPIED
        await _persist(session, *claims)
        await session.commit()
        return Seeded(
            teacher=teacher,
            student=student,
            tasks=tasks,
            assignments=assignments,
            claims=claims,
        )


def _service(
    at: datetime,
    *,
    business_timezone: str = "Asia/Shanghai",
    events: InMemoryEventCollector | None = None,
    daily_abandon_limit: int = DAILY_ABANDON_LIMIT,
) -> AbandonService:
    return AbandonService(
        clock=FrozenClock(at),
        business_timezone=business_timezone,
        events=events if events is not None else InMemoryEventCollector(),
        daily_abandon_limit=daily_abandon_limit,
    )


async def _abandoned_today(
    factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    *,
    window_start: datetime,
    window_end: datetime,
) -> int:
    """The durable fact the daily cap reads: this user's ABANDONED claims
    whose terminal instant falls in [window_start, window_end)."""
    async with factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(AssignmentClaim)
            .where(
                AssignmentClaim.user_id == user_id,
                AssignmentClaim.status == ClaimStatus.ABANDONED,
                AssignmentClaim.terminal_at >= window_start,
                AssignmentClaim.terminal_at < window_end,
            )
        )
    return count or 0


# --- concurrency harness ---------------------------------------------------------


@dataclass(slots=True)
class AbandonOutcome:
    """One abandon attempt's terminal state: a claim, a business error, or
    — never, if the service is correct — an unexpected exception."""

    claim: AssignmentClaim | None = None
    error: BusinessError | None = None
    unexpected: BaseException | None = None


async def _abandon_one(
    service: AbandonService,
    factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    claim_id: UUID,
    start: asyncio.Event,
) -> AbandonOutcome:
    async with factory() as session:
        # Warm the pooled connection BEFORE the barrier (same rationale as
        # test_claim_concurrency.py): connection setup must not serialize
        # the racers and hide the lock interleaving under test.
        await session.execute(text("SELECT 1"))
        await start.wait()
        try:
            claim = await service.abandon_claim(session, user_id, claim_id)
        except BusinessError as exc:
            return AbandonOutcome(error=exc)
        except Exception as exc:  # the "no 500" failure mode
            return AbandonOutcome(unexpected=exc)
        return AbandonOutcome(claim=claim)


async def _run_concurrently(
    service: AbandonService,
    factory: async_sessionmaker[AsyncSession],
    calls: list[tuple[UUID, UUID]],
) -> list[AbandonOutcome]:
    start = asyncio.Event()
    tasks = [
        asyncio.create_task(_abandon_one(service, factory, user_id, claim_id, start))
        for user_id, claim_id in calls
    ]
    await asyncio.sleep(0.05)  # let every coroutine reach the barrier
    start.set()
    return list(await asyncio.gather(*tasks))


async def _assignment_status(
    factory: async_sessionmaker[AsyncSession], assignment_id: UUID
) -> AssignmentAvailability:
    async with factory() as session:
        row = await session.get(Assignment, assignment_id)
        assert row is not None
        return AssignmentAvailability(row.availability_status)


# --- daily cap: two per BUSINESS_TIMEZONE natural day ------------------------------


@pytest.mark.integration
async def test_third_abandon_same_business_day_is_rejected(
    db_engine: AsyncEngine,
) -> None:
    """Spec §8.5: at most 2 abandons per BUSINESS_TIMEZONE natural day. The
    1st and 2nd succeed (Claim -> ABANDONED with terminal_at, Assignment ->
    AVAILABLE); the 3rd is ABANDON_LIMIT_REACHED with the limit in details
    and leaves the claim and its assignment untouched."""
    factory = _factory(db_engine)
    events = InMemoryEventCollector()
    service = _service(_NOW, events=events)
    run = uuid4().hex[:8]

    seeded = await _seed(
        factory,
        run=run,
        statuses=[ClaimStatus.CLAIMED] * 3,
        anchor=_NOW,
    )
    task_ids = [task.id for task in seeded.tasks]
    user_ids = [seeded.teacher.id, seeded.student.id]
    try:
        for index in range(2):
            async with factory() as session:
                claim = await service.abandon_claim(
                    session, seeded.student.id, seeded.claims[index].id
                )
            assert claim.status == ClaimStatus.ABANDONED
            assert claim.terminal_at == _NOW
            assignment = seeded.assignments[index]
            loaded = await _assignment_status(factory, assignment.id)
            assert loaded == AssignmentAvailability.AVAILABLE

        async with factory() as session:
            with pytest.raises(BusinessError) as exc_info:
                await service.abandon_claim(
                    session, seeded.student.id, seeded.claims[2].id
                )
        assert exc_info.value.code == ErrorCode.ABANDON_LIMIT_REACHED
        assert exc_info.value.status_code < 500
        assert exc_info.value.details["limit"] == DAILY_ABANDON_LIMIT

        # Nothing leaked from the refused third abandon.
        async with factory() as session:
            loaded = await session.get(AssignmentClaim, seeded.claims[2].id)
            assert loaded is not None
            assert loaded.status == ClaimStatus.CLAIMED
            assert loaded.terminal_at is None
        assert (
            await _assignment_status(factory, seeded.assignments[2].id)
            == AssignmentAvailability.OCCUPIED
        )
        assert len(events.of_type(CLAIM_ABANDONED)) == 2
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- natural-day window: business midnight, not UTC midnight, not 24h ----------------


@pytest.mark.integration
async def test_counter_resets_at_shanghai_midnight_not_utc_or_24h(
    db_engine: AsyncEngine,
) -> None:
    """Spec §0/§44.14: the natural day is BUSINESS_TIMEZONE's. Two abandons
    at 17:00Z Jan 15 and 06:00Z Jan 16 share ONE Asia/Shanghai day (Jan 16)
    across two different UTC dates, so the third at 07:00Z is refused; only
    after 16:00Z (Shanghai midnight) does the next abandon succeed. A
    UTC-date counter would reset between the first two; a rolling 24h
    counter would still count the first at 16:30Z."""
    factory = _factory(db_engine)
    run = uuid4().hex[:8]

    seeded = await _seed(
        factory,
        run=run,
        statuses=[ClaimStatus.CLAIMED] * 3,
        anchor=_NOW,
    )
    task_ids = [task.id for task in seeded.tasks]
    user_ids = [seeded.teacher.id, seeded.student.id]
    try:
        # Jan 16 01:00 +08 (UTC Jan 15): 1st abandon of the business day.
        async with factory() as session:
            await _service(_NOW).abandon_claim(
                session, seeded.student.id, seeded.claims[0].id
            )
        # Jan 16 14:00 +08 (UTC Jan 16): same business day, different UTC
        # date — a UTC-date counter would see only one abandon so far.
        async with factory() as session:
            await _service(datetime(2026, 1, 16, 6, 0, tzinfo=UTC)).abandon_claim(
                session, seeded.student.id, seeded.claims[1].id
            )
        # Jan 16 15:00 +08: daily cap reached.
        async with factory() as session:
            with pytest.raises(BusinessError) as exc_info:
                await _service(datetime(2026, 1, 16, 7, 0, tzinfo=UTC)).abandon_claim(
                    session, seeded.student.id, seeded.claims[2].id
                )
        assert exc_info.value.code == ErrorCode.ABANDON_LIMIT_REACHED

        # Jan 17 00:30 +08 (16:30Z): past local midnight — 22.5h after the
        # FIRST abandon but a new natural day. A rolling 24h window would
        # still refuse; the business-day window must allow it.
        async with factory() as session:
            claim = await _service(
                datetime(2026, 1, 16, 16, 30, tzinfo=UTC)
            ).abandon_claim(session, seeded.student.id, seeded.claims[2].id)
        assert claim.status == ClaimStatus.ABANDONED
        assert claim.terminal_at == datetime(2026, 1, 16, 16, 30, tzinfo=UTC)
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


@pytest.mark.integration
async def test_dst_short_day_window_follows_local_midnight(
    db_engine: AsyncEngine,
) -> None:
    """Spec §38.9's DST case: with BUSINESS_TIMEZONE=America/New_York the
    2026-03-08 spring-forward makes the natural day 23 hours (05:00Z ->
    04:00Z next day). An abandon at 03:30Z Mar 9 is still local Mar 8 and
    counts toward the cap; 30 minutes later (04:00Z = local midnight) the
    counter has reset. This proves no +8 offset and no fixed 24h window is
    baked in."""
    factory = _factory(db_engine)
    run = uuid4().hex[:8]

    seeded = await _seed(
        factory,
        run=run,
        statuses=[ClaimStatus.CLAIMED] * 3,
        anchor=_NY_DAY_START,
    )
    task_ids = [task.id for task in seeded.tasks]
    user_ids = [seeded.teacher.id, seeded.student.id]
    try:
        tz = "America/New_York"
        # Local Mar 8 01:00 EST — first abandon of the short day.
        async with factory() as session:
            await _service(_NY_DAY_START, business_timezone=tz).abandon_claim(
                session, seeded.student.id, seeded.claims[0].id
            )
        # Local Mar 8 23:30 EDT (22.5h later, UTC date already Mar 9): same
        # local day, so the second abandon fills the cap.
        async with factory() as session:
            await _service(
                datetime(2026, 3, 9, 3, 30, tzinfo=UTC), business_timezone=tz
            ).abandon_claim(session, seeded.student.id, seeded.claims[1].id)
        # Local Mar 8 23:45 EDT: refused — still the same 23-hour day.
        async with factory() as session:
            with pytest.raises(BusinessError) as exc_info:
                await _service(
                    datetime(2026, 3, 9, 3, 45, tzinfo=UTC), business_timezone=tz
                ).abandon_claim(session, seeded.student.id, seeded.claims[2].id)
        assert exc_info.value.code == ErrorCode.ABANDON_LIMIT_REACHED

        # 04:00Z = local Mar 9 00:00 EDT: only 15 minutes later, but a new
        # natural day — the abandon that was refused above now succeeds.
        async with factory() as session:
            claim = await _service(
                datetime(2026, 3, 9, 4, 0, tzinfo=UTC), business_timezone=tz
            ).abandon_claim(session, seeded.student.id, seeded.claims[2].id)
        assert claim.status == ClaimStatus.ABANDONED
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- concurrency: same claim counts once -------------------------------------------


@pytest.mark.integration
async def test_concurrent_abandons_of_same_claim_count_once(
    db_engine: AsyncEngine,
) -> None:
    """Spec §8.5 idempotency under contention: two simultaneous abandons of
    one claim land as ONE transition plus one idempotent replay — no error,
    no double count, one audit event, and the claim is ABANDONED exactly
    once with its assignment AVAILABLE exactly once."""
    factory = _factory(db_engine)
    events = InMemoryEventCollector()
    service = _service(_NOW, events=events)
    run = uuid4().hex[:8]

    seeded = await _seed(
        factory,
        run=run,
        statuses=[ClaimStatus.CLAIMED] * 3,
        anchor=_NOW,
    )
    task_ids = [task.id for task in seeded.tasks]
    user_ids = [seeded.teacher.id, seeded.student.id]
    try:
        outcomes = await _run_concurrently(
            service,
            factory,
            [(seeded.student.id, seeded.claims[0].id)] * 2,
        )

        assert not [o for o in outcomes if o.unexpected is not None]
        assert not [o for o in outcomes if o.error is not None]
        assert all(o.claim is not None for o in outcomes)
        assert {o.claim.id for o in outcomes if o.claim is not None} == {
            seeded.claims[0].id
        }
        assert all(
            o.claim.status == ClaimStatus.ABANDONED
            for o in outcomes
            if o.claim is not None
        )
        assert len({o.claim.terminal_at for o in outcomes if o.claim is not None}) == 1
        assert len(events.of_type(CLAIM_ABANDONED)) == 1
        assert (
            await _assignment_status(factory, seeded.assignments[0].id)
            == AssignmentAvailability.AVAILABLE
        )

        # The daily count moved by exactly one: a second abandon today is
        # still allowed, and only the third is refused.
        async with factory() as session:
            await service.abandon_claim(session, seeded.student.id, seeded.claims[1].id)
        async with factory() as session:
            with pytest.raises(BusinessError) as exc_info:
                await service.abandon_claim(
                    session, seeded.student.id, seeded.claims[2].id
                )
        assert exc_info.value.code == ErrorCode.ABANDON_LIMIT_REACHED
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


@pytest.mark.integration
async def test_concurrent_abandons_of_different_claims_cannot_bypass_limit(
    db_engine: AsyncEngine,
) -> None:
    """Spec §8.5: the daily-cap check is atomic. With one daily slot left
    (one ABANDONED claim earlier today, limit 2) and two DIFFERENT claims
    abandoned simultaneously, exactly one wins; the loser gets
    ABANDON_LIMIT_REACHED, and the database ends the day with exactly two
    ABANDONED claims and each assignment consistent with its claim."""
    factory = _factory(db_engine)
    service = _service(_NOW)
    run = uuid4().hex[:8]

    seeded = await _seed(
        factory,
        run=run,
        statuses=[
            ClaimStatus.ABANDONED,  # used one daily slot an hour ago
            ClaimStatus.CLAIMED,
            ClaimStatus.CLAIMED,
        ],
        anchor=_NOW,
    )
    task_ids = [task.id for task in seeded.tasks]
    user_ids = [seeded.teacher.id, seeded.student.id]
    try:
        outcomes = await _run_concurrently(
            service,
            factory,
            [
                (seeded.student.id, seeded.claims[1].id),
                (seeded.student.id, seeded.claims[2].id),
            ],
        )

        successes = [o for o in outcomes if o.claim is not None]
        failures = [o for o in outcomes if o.error is not None]
        assert not [o for o in outcomes if o.unexpected is not None]
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0].error is not None
        assert failures[0].error.code == ErrorCode.ABANDON_LIMIT_REACHED
        assert failures[0].error.status_code < 500

        # Winner terminal + released; loser untouched.
        winner_index, loser_index = (
            (1, 2) if successes[0].claim.id == seeded.claims[1].id else (2, 1)
        )
        async with factory() as session:
            winner = await session.get(AssignmentClaim, seeded.claims[winner_index].id)
            assert winner is not None
            assert winner.status == ClaimStatus.ABANDONED
            assert winner.terminal_at == _NOW
            loser = await session.get(AssignmentClaim, seeded.claims[loser_index].id)
            assert loser is not None
            assert loser.status == ClaimStatus.CLAIMED
            assert loser.terminal_at is None
        assert (
            await _assignment_status(factory, seeded.assignments[winner_index].id)
            == AssignmentAvailability.AVAILABLE
        )
        assert (
            await _assignment_status(factory, seeded.assignments[loser_index].id)
            == AssignmentAvailability.OCCUPIED
        )

        # Exactly two abandons landed inside the business day.
        day_start = datetime(2026, 1, 15, 16, 0, tzinfo=UTC)  # Jan 16 00:00 +08
        day_end = datetime(2026, 1, 16, 16, 0, tzinfo=UTC)
        assert (
            await _abandoned_today(
                factory,
                seeded.student.id,
                window_start=day_start,
                window_end=day_end,
            )
            == DAILY_ABANDON_LIMIT
        )
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- reallocation: released, re-assignable, never back to the abandoner --------------


@pytest.mark.integration
async def test_abandoned_assignment_reallocated_but_never_back_to_abandoner(
    db_engine: AsyncEngine,
) -> None:
    """Spec §8.5/§8.2: A abandons Assignment X -> X is AVAILABLE; B receives
    X through the normal random claim; once X is released again, A claiming
    the same task is refused with NO_ASSIGNMENT_AVAILABLE — the exclusion
    subquery keeps X away from its abandoner for the task's lifecycle."""
    factory = _factory(db_engine)
    service = _service(_NOW)
    claimer = ClaimService(clock=FrozenClock(_NOW))
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        async with factory() as session:
            teacher = _user(username=f"t{run}", role=Role.TEACHER)
            student_a = _user(username=f"2025{run}001")
            student_b = _user(username=f"2025{run}002")
            await _persist(session, teacher, student_a, student_b)
            # One task whose ONLY assignment is X: every random claim below
            # must resolve to it deterministically.
            task = _task(teacher, title="唯一单元任务")
            await _persist(session, task)
            assignment_x = _assignment(task, keyword="考研英语")
            await _persist(session, assignment_x)
            claim_a = _claim(
                assignment_x,
                student_a,
                status=ClaimStatus.CLAIMED,
                claimed_at=_NOW - timedelta(hours=2),
            )
            assignment_x.availability_status = AssignmentAvailability.OCCUPIED
            await _persist(session, claim_a)
            await session.commit()
            task_ids.append(task.id)
            user_ids.extend([teacher.id, student_a.id, student_b.id])

        # A abandons: the unit returns to AVAILABLE.
        async with factory() as session:
            await service.abandon_claim(session, student_a.id, claim_a.id)
        assert (
            await _assignment_status(factory, assignment_x.id)
            == AssignmentAvailability.AVAILABLE
        )

        # B claims the task and receives X.
        async with factory() as session:
            claim_b = await claimer.claim_random_assignment(
                session, student_b.id, task.id
            )
        assert claim_b.assignment_id == assignment_x.id
        assert claim_b.status == ClaimStatus.CLAIMED

        # B releases X again (through the same abandon service).
        async with factory() as session:
            await service.abandon_claim(session, student_b.id, claim_b.id)
        assert (
            await _assignment_status(factory, assignment_x.id)
            == AssignmentAvailability.AVAILABLE
        )

        # A can never randomly receive X again: the task's only candidate is
        # excluded for A, so the claim refuses instead of recycling.
        async with factory() as session:
            with pytest.raises(BusinessError) as exc_info:
                await claimer.claim_random_assignment(session, student_a.id, task.id)
        assert exc_info.value.code == ErrorCode.NO_ASSIGNMENT_AVAILABLE
        assert exc_info.value.status_code < 500
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- idempotent replay ---------------------------------------------------------------


@pytest.mark.integration
async def test_abandon_replay_returns_same_terminal_result_without_recount(
    db_engine: AsyncEngine,
) -> None:
    """Spec §8.5: re-calling abandon on an already-ABANDONED claim returns
    the same terminal result (no error, same terminal_at, no second audit
    event) and does not consume another daily slot."""
    factory = _factory(db_engine)
    events = InMemoryEventCollector()
    service = _service(_NOW, events=events)
    run = uuid4().hex[:8]

    seeded = await _seed(
        factory,
        run=run,
        statuses=[ClaimStatus.CLAIMED] * 3,
        anchor=_NOW,
    )
    task_ids = [task.id for task in seeded.tasks]
    user_ids = [seeded.teacher.id, seeded.student.id]
    try:
        async with factory() as session:
            first = await service.abandon_claim(
                session, seeded.student.id, seeded.claims[0].id
            )
        assert first.status == ClaimStatus.ABANDONED

        async with factory() as session:
            replay = await service.abandon_claim(
                session, seeded.student.id, seeded.claims[0].id
            )
        assert replay.id == first.id
        assert replay.status == ClaimStatus.ABANDONED
        assert replay.terminal_at == first.terminal_at == _NOW
        assert len(events.of_type(CLAIM_ABANDONED)) == 1

        # The replay consumed no slot: a second and then a third abandon
        # today behave exactly as if the replay never happened.
        async with factory() as session:
            await service.abandon_claim(session, seeded.student.id, seeded.claims[1].id)
        async with factory() as session:
            with pytest.raises(BusinessError) as exc_info:
                await service.abandon_claim(
                    session, seeded.student.id, seeded.claims[2].id
                )
        assert exc_info.value.code == ErrorCode.ABANDON_LIMIT_REACHED
        assert len(events.of_type(CLAIM_ABANDONED)) == 2
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- guards: non-actionable status, ownership, account -------------------------------


@pytest.mark.integration
async def test_under_review_claim_cannot_be_abandoned(db_engine: AsyncEngine) -> None:
    """Spec §8.2 ruling: a claim whose file is already submitted and under
    review is not a student abandon action — VALIDATING/UNDER_REVIEW do
    not even occupy a quota slot, and abandoning mid-review would pull a
    submitted artifact out from under the review pipeline. The abandon is
    CLAIM_NOT_ABANDONABLE (4xx), nothing changes, and the daily counter is
    not consumed (the same student can still abandon an actionable claim
    afterwards)."""
    factory = _factory(db_engine)
    events = InMemoryEventCollector()
    service = _service(_NOW, events=events)
    run = uuid4().hex[:8]

    seeded = await _seed(
        factory,
        run=run,
        statuses=[ClaimStatus.UNDER_REVIEW, ClaimStatus.CLAIMED],
        anchor=_NOW,
    )
    task_ids = [task.id for task in seeded.tasks]
    user_ids = [seeded.teacher.id, seeded.student.id]
    try:
        async with factory() as session:
            with pytest.raises(BusinessError) as exc_info:
                await service.abandon_claim(
                    session, seeded.student.id, seeded.claims[0].id
                )
        assert exc_info.value.code == ErrorCode.CLAIM_NOT_ABANDONABLE
        assert exc_info.value.status_code < 500
        assert exc_info.value.details["claim_status"] == ClaimStatus.UNDER_REVIEW.value

        async with factory() as session:
            loaded = await session.get(AssignmentClaim, seeded.claims[0].id)
            assert loaded is not None
            assert loaded.status == ClaimStatus.UNDER_REVIEW
            assert loaded.terminal_at is None
        assert (
            await _assignment_status(factory, seeded.assignments[0].id)
            == AssignmentAvailability.OCCUPIED
        )
        assert events.of_type(CLAIM_ABANDONED) == []

        # The refusal consumed no daily slot.
        async with factory() as session:
            claim = await service.abandon_claim(
                session, seeded.student.id, seeded.claims[1].id
            )
        assert claim.status == ClaimStatus.ABANDONED
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


@pytest.mark.integration
async def test_revision_required_claim_is_abandonable(db_engine: AsyncEngine) -> None:
    """Spec §8.2/§8.5: REVISION_REQUIRED still needs student action, so it
    IS abandonable — abandoning it releases the unit and frees the quota
    slot exactly like a CLAIMED claim."""
    factory = _factory(db_engine)
    service = _service(_NOW)
    run = uuid4().hex[:8]

    seeded = await _seed(
        factory,
        run=run,
        statuses=[ClaimStatus.REVISION_REQUIRED],
        anchor=_NOW,
    )
    task_ids = [task.id for task in seeded.tasks]
    user_ids = [seeded.teacher.id, seeded.student.id]
    try:
        async with factory() as session:
            claim = await service.abandon_claim(
                session, seeded.student.id, seeded.claims[0].id
            )
        assert claim.status == ClaimStatus.ABANDONED
        assert claim.terminal_at == _NOW
        assert (
            await _assignment_status(factory, seeded.assignments[0].id)
            == AssignmentAvailability.AVAILABLE
        )
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


@pytest.mark.integration
async def test_abandon_guards_missing_foreign_and_suspended(
    db_engine: AsyncEngine,
) -> None:
    """A missing claim is 404 NOT_FOUND; another student's claim is 403
    PERMISSION_DENIED and stays untouched; a non-ACTIVE account is refused
    with ACCOUNT_NOT_ACTIVE (the same identity precedent as claiming)."""
    factory = _factory(db_engine)
    service = _service(_NOW)
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        async with factory() as session:
            teacher = _user(username=f"t{run}", role=Role.TEACHER)
            owner = _user(username=f"2025{run}001")
            outsider = _user(username=f"2025{run}002")
            suspended = _user(username=f"2025{run}003", status=UserStatus.SUSPENDED)
            await _persist(session, teacher, owner, outsider, suspended)
            task = _task(teacher)
            await _persist(session, task)
            owned = _assignment(task, keyword="考研英语")
            foreign = _assignment(task, keyword="考研政治")
            await _persist(session, owned, foreign)
            owner_claim = _claim(
                owned,
                owner,
                status=ClaimStatus.CLAIMED,
                claimed_at=_NOW - timedelta(hours=2),
            )
            suspended_claim = _claim(
                foreign,
                suspended,
                status=ClaimStatus.CLAIMED,
                claimed_at=_NOW - timedelta(hours=2),
            )
            owned.availability_status = AssignmentAvailability.OCCUPIED
            foreign.availability_status = AssignmentAvailability.OCCUPIED
            await _persist(session, owner_claim, suspended_claim)
            await session.commit()
            task_ids.append(task.id)
            user_ids.extend([teacher.id, owner.id, outsider.id, suspended.id])

        async with factory() as session:
            with pytest.raises(BusinessError) as missing:
                await service.abandon_claim(session, outsider.id, uuid4())
        assert missing.value.code == ErrorCode.NOT_FOUND
        assert missing.value.status_code == 404

        async with factory() as session:
            with pytest.raises(BusinessError) as foreign_error:
                await service.abandon_claim(session, outsider.id, owner_claim.id)
        assert foreign_error.value.code == ErrorCode.PERMISSION_DENIED
        assert foreign_error.value.status_code == 403
        async with factory() as session:
            loaded = await session.get(AssignmentClaim, owner_claim.id)
            assert loaded is not None
            assert loaded.status == ClaimStatus.CLAIMED
            assert loaded.terminal_at is None
        assert (
            await _assignment_status(factory, owned.id)
            == AssignmentAvailability.OCCUPIED
        )

        async with factory() as session:
            with pytest.raises(BusinessError) as inactive:
                await service.abandon_claim(session, suspended.id, suspended_claim.id)
        assert inactive.value.code == ErrorCode.ACCOUNT_NOT_ACTIVE
        assert inactive.value.status_code == 403
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- no points side effects -----------------------------------------------------------


@pytest.mark.integration
async def test_abandon_touches_no_points_or_reward_state(
    db_engine: AsyncEngine,
) -> None:
    """Spec §8.5: abandoning does not deduct points — no reward lock, no
    locked points, no ledger-like side channel. The only state that moves
    is the claim's terminal pair (status, terminal_at) and the
    assignment's availability; every snapshot the claim froze at claim
    time is byte-identical afterwards."""
    factory = _factory(db_engine)
    events = InMemoryEventCollector()
    service = _service(_NOW, events=events)
    run = uuid4().hex[:8]

    seeded = await _seed(
        factory,
        run=run,
        statuses=[ClaimStatus.CLAIMED],
        anchor=_NOW,
    )
    task_ids = [task.id for task in seeded.tasks]
    user_ids = [seeded.teacher.id, seeded.student.id]
    try:
        async with factory() as session:
            claim = await service.abandon_claim(
                session, seeded.student.id, seeded.claims[0].id
            )

        assert claim.reward_lock_status == RewardLockStatus.NONE
        assert claim.reward_tier_locked is None
        assert claim.reward_locked_at is None
        assert claim.locked_reward_points is None
        assert claim.latest_submission_id is None

        async with factory() as session:
            loaded = await session.get(AssignmentClaim, seeded.claims[0].id)
            assert loaded is not None
            # Snapshots survive the terminal transition untouched.
            assert loaded.base_reward_points_snapshot == 100
            assert loaded.submission_schema_version == 1
            assert loaded.reward_policy_snapshot == {
                "version": 1,
                "ladder_fractions": ["1", "0.8"],
            }
            assert loaded.deadline_at == seeded.claims[0].deadline_at
            assert loaded.grace_deadline_at == seeded.claims[0].grace_deadline_at
            assert loaded.claimed_at == seeded.claims[0].claimed_at
            assert loaded.reward_lock_status == RewardLockStatus.NONE
            assert loaded.locked_reward_points is None

            # No new claim rows appeared; the one row just changed status.
            claim_ids = (
                await session.scalars(
                    select(AssignmentClaim.id).where(
                        AssignmentClaim.user_id == seeded.student.id
                    )
                )
            ).all()
            assert claim_ids == [seeded.claims[0].id]

        # The only observable side channel is the single audit event.
        assert [event.event_type for event in events.events] == [CLAIM_ABANDONED]
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)
