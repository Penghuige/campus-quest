# backend/tests/integration/tasks/test_submit_expire_race.py
"""Claim-expiry service, candidate scan, and submit-vs-expire races against
real PostgreSQL (plan 07 T6; spec §8.2, §11.4, §11.5/§26 ruling;
backend-engineering §7: independent sessions and connections).

Every scenario drives the full ``ClaimService.expire_claim_if_due``
transaction — naive-now guard, claim row FOR UPDATE, the outcome ladder
(MISSING -> ALREADY_TERMINAL -> PROTECTED -> NOT_DUE -> VALID_SUBMISSION ->
EXPIRED), assignment release OCCUPIED -> AVAILABLE only (RETIRED/COMPLETED
sticky), one ``CLAIM_EXPIRED`` audit event, exactly one commit — plus the
worker's candidate scan (``collect_due_claim_ids``), and asserts:

- **Inclusive boundary:** a claim at exactly ``now == effective deadline``
  is due; NOT_DUE is strictly ``now < deadline``.
- **Effective deadline:** ``max(grace_deadline_at, revision_deadline_at)``
  — a review extension defers expiry, an earlier revision deadline never
  shortens grace, a future grace is not due.
- **Protection:** VALIDATING/UNDER_REVIEW claims are PROTECTED (the review
  pipeline owns them); the assignment stays OCCUPIED.
- **Strict §11.5/§26 reading (plan-04 amendment 2):** only a machine-
  VALIDATED submission protects a due claim. The ``ValidSubmissionInspector``
  port is the seam; the default ``NoValidSubmissionsInspector`` answers
  False, so a due claim whose in-window submission is not yet VALIDATED
  expires — the merged branch wires the real VALIDATED-reading inspector
  at that exact constructor argument.
- **No payout (spec §11.4):** expiry touches only the claim's terminal
  pair and the assignment's availability; a PROVISIONAL reward lock, its
  tier/points/instants, and every claim-time snapshot survive untouched
  (reversals own the ledger).
- **Idempotency and stickiness:** replaying against a terminal claim is
  an ALREADY_TERMINAL no-op (no second event, no release flip); a RETIRED
  or COMPLETED assignment is never resurrected.
- **Races (waiter-proof via pg_stat_activity polling):** a finalize that
  commits while expiry waits on the claim-row lock is always observed —
  through the status variant (PROTECTED) and the inspector variant
  (VALID_SUBMISSION); an expiry that holds the lock makes a concurrent
  finalize abort its flip (the submission can no longer own an EXPIRED
  claim); three simultaneous expiries land exactly one EXPIRED plus two
  ALREADY_TERMINAL replays.

Harness notes (same shape as test_claim_concurrency.py / test_abandon.py):
seeding, mutations, and assertions use independent committed sessions from
the engine factory; every racer warms its pooled connection with ``SELECT
1`` before parking; the waiter-proof helper polls ``pg_stat_activity`` for
backends in Lock wait on an ``assignment_claims`` query, so a test never
mistakes "slow" for "blocked"; every test removes its rows with explicit
committed DELETEs in ``finally`` (claims -> assignments -> tasks -> users,
the FK order); usernames embed a per-run token.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.clock import FrozenClock
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import InMemoryEventCollector
from app.modules.identity.models import User
from app.modules.tasks.claim_service import (
    CLAIM_EXPIRED,
    EXPIRY_ACTIONABLE_STATUSES,
    ClaimService,
    ExpireResult,
    ExpiryOutcome,
    NoValidSubmissionsInspector,
    effective_expiry_deadline,
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
from app.workers.jobs.expire_claims import collect_due_claim_ids

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


def _claim(
    assignment: Assignment,
    user: User,
    *,
    status: ClaimStatus,
    claimed_at: datetime,
    grace_deadline_at: datetime,
    revision_deadline_at: datetime | None = None,
    terminal_at: datetime | None = None,
    reward_lock_status: RewardLockStatus = RewardLockStatus.NONE,
    reward_tier_locked: int | None = None,
    reward_locked_at: datetime | None = None,
    locked_reward_points: int | None = None,
    latest_submission_id: UUID | None = None,
) -> AssignmentClaim:
    """A claim row from "before this test", carrying plausible snapshot
    values and the exact expiry-relevant deadline fields."""
    return AssignmentClaim(
        assignment_id=assignment.id,
        task_id=assignment.task_id,
        user_id=user.id,
        status=status,
        claimed_at=claimed_at,
        deadline_at=grace_deadline_at - timedelta(minutes=1440),
        grace_deadline_at=grace_deadline_at,
        reward_policy_snapshot={"version": 1, "ladder_fractions": ["1", "0.8"]},
        base_reward_points_snapshot=100,
        submission_schema_version=1,
        reward_lock_status=reward_lock_status,
        reward_tier_locked=reward_tier_locked,
        reward_locked_at=reward_locked_at,
        locked_reward_points=locked_reward_points,
        latest_submission_id=latest_submission_id,
        revision_deadline_at=revision_deadline_at,
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
class ClaimSpec:
    """One seeded claim's expiry-relevant shape (one task + one assignment
    per claim keeps both partial unique indexes free)."""

    status: ClaimStatus
    grace_at: datetime
    revision_at: datetime | None = None
    # None derives the availability the producing flow would have left.
    assignment_status: AssignmentAvailability | None = None
    reward_lock_status: RewardLockStatus = RewardLockStatus.NONE
    reward_tier_locked: int | None = None
    reward_locked_at: datetime | None = None
    locked_reward_points: int | None = None
    latest_submission_id: UUID | None = None
    terminal_at: datetime | None = None


@dataclass(slots=True)
class Seeded:
    teacher: User
    student: User
    tasks: list[Task]
    assignments: list[Assignment]
    claims: list[AssignmentClaim]


def _derived_assignment_status(status: ClaimStatus) -> AssignmentAvailability:
    if status is ClaimStatus.COMPLETED:
        return AssignmentAvailability.COMPLETED
    if status in (ClaimStatus.ABANDONED, ClaimStatus.EXPIRED):
        return AssignmentAvailability.AVAILABLE
    return AssignmentAvailability.OCCUPIED


async def _seed_claims(
    factory: async_sessionmaker[AsyncSession],
    *,
    run: str,
    specs: list[ClaimSpec],
) -> Seeded:
    async with factory() as session:
        teacher = _user(username=f"t{run}", role=Role.TEACHER)
        student = _user(username=f"2025{run}001")
        await _persist(session, teacher, student)
        tasks = [_task(teacher, title=f"任务{i}") for i in range(len(specs))]
        await _persist(session, *tasks)
        assignments = [
            _assignment(task, keyword=f"考研数学{i}") for i, task in enumerate(tasks)
        ]
        await _persist(session, *assignments)
        claims: list[AssignmentClaim] = []
        for assignment, spec in zip(assignments, specs, strict=True):
            availability = (
                spec.assignment_status
                if spec.assignment_status is not None
                else _derived_assignment_status(spec.status)
            )
            assignment.availability_status = availability
            claims.append(
                _claim(
                    assignment,
                    student,
                    status=spec.status,
                    claimed_at=_NOW - timedelta(days=5),
                    grace_deadline_at=spec.grace_at,
                    revision_deadline_at=spec.revision_at,
                    terminal_at=spec.terminal_at,
                    reward_lock_status=spec.reward_lock_status,
                    reward_tier_locked=spec.reward_tier_locked,
                    reward_locked_at=spec.reward_locked_at,
                    locked_reward_points=spec.locked_reward_points,
                    latest_submission_id=spec.latest_submission_id,
                )
            )
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
    *,
    events: InMemoryEventCollector | None = None,
    inspector: Any = None,
) -> ClaimService:
    return ClaimService(
        clock=FrozenClock(_NOW),
        event_publisher=events,
        valid_submission_inspector=inspector,
    )


# --- inspector fakes ---------------------------------------------------------------


@dataclass(slots=True)
class RecordingInspector:
    """Configurable ValidSubmissionInspector fake that records its calls."""

    answers: dict[UUID, bool] = field(default_factory=dict)
    calls: list[UUID] = field(default_factory=list)

    async def has_valid_submission(self, db: AsyncSession, claim_id: UUID) -> bool:
        self.calls.append(claim_id)
        return self.answers.get(claim_id, False)


@dataclass(slots=True)
class ParkingInspector:
    """Inspector that parks while the expiry holds the claim-row lock.

    ``entered`` fires once the service consults the inspector — which the
    expiry-first race proves happens AFTER the FOR UPDATE — and the call
    only returns once the test sets ``release``."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    calls: list[UUID] = field(default_factory=list)

    async def has_valid_submission(self, db: AsyncSession, claim_id: UUID) -> bool:
        self.calls.append(claim_id)
        self.entered.set()
        await self.release.wait()
        return False


# --- concurrency harness (waiter-proof) --------------------------------------------


async def _expire_one(
    service: ClaimService,
    factory: async_sessionmaker[AsyncSession],
    claim_id: UUID,
    now: datetime,
    start: asyncio.Event | None = None,
) -> ExpireResult:
    async with factory() as session:
        # Warm the pooled connection BEFORE the barrier (same rationale as
        # test_claim_concurrency.py): connection setup must not serialize
        # the racers and hide the lock interleaving under test.
        await session.execute(text("SELECT 1"))
        if start is not None:
            await start.wait()
        return await service.expire_claim_if_due(session, claim_id, now)


@dataclass(slots=True)
class FinalizeOutcome:
    """The simulated finalize's terminal state: it flipped the claim to
    UNDER_REVIEW, or aborted because the locked row was no longer
    actionable (an EXPIRED claim can no longer be owned by a submission)."""

    aborted: bool
    observed_status: ClaimStatus | None
    flipped_to: ClaimStatus | None


async def _guarded_finalize(
    factory: async_sessionmaker[AsyncSession], claim_id: UUID
) -> FinalizeOutcome:
    """The submission pipeline's finalize contract, minimal form: take the
    claim-row FOR UPDATE, judge status on the locked row, and flip to
    UNDER_REVIEW only while the claim is still actionable."""
    async with factory() as session:
        await session.execute(text("SELECT 1"))
        claim = await session.scalar(
            select(AssignmentClaim)
            .where(AssignmentClaim.id == claim_id)
            .with_for_update()
        )
        if claim is None:
            return FinalizeOutcome(aborted=True, observed_status=None, flipped_to=None)
        observed = ClaimStatus(claim.status)
        if observed not in EXPIRY_ACTIONABLE_STATUSES:
            # The claim expired under the finalize: abort without writing.
            await session.rollback()
            return FinalizeOutcome(
                aborted=True, observed_status=observed, flipped_to=None
            )
        claim.status = ClaimStatus.UNDER_REVIEW
        await session.commit()
        return FinalizeOutcome(
            aborted=False,
            observed_status=observed,
            flipped_to=ClaimStatus.UNDER_REVIEW,
        )


async def _await_row_lock_waiter(
    factory: async_sessionmaker[AsyncSession],
    *,
    count: int = 1,
    timeout: float = 5.0,
) -> None:
    """Poll pg_stat_activity until ``count`` backends sit in an active
    Lock wait in this database — deterministic proof the waiters PARKED
    on the row lock, never that they are merely slow to arrive.

    The wait-signature level (not the query text, not the specific
    wait_event) is the predicate: asyncpg prepares and caches statements
    per connection, and a Bind/Execute against an already-parsed
    statement leaves ``query`` showing a stale earlier statement
    (observed: a backend blocked mid-``FOR UPDATE`` reporting ``query =
    'SELECT 1'``); and with three racers on one row PostgreSQL parks the
    first follower on the holder's ``transactionid`` and the second on
    the tuple itself (``wait_event = 'tuple'``, multi-xact). The system
    backends (autovacuum, wal writer, ...) wait on Activity, never Lock.
    """
    deadline = time.monotonic() + timeout
    async with factory() as session:
        while True:
            blocked = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' "
                    "AND state = 'active' "
                    "AND datname = current_database() "
                    "AND pid <> pg_backend_pid()"
                )
            )
            if (blocked or 0) >= count:
                return
            if time.monotonic() > deadline:
                rows = (
                    await session.execute(
                        text(
                            "select pid, state, wait_event_type, wait_event, "
                            "left(query, 80) as q from pg_stat_activity "
                            "where pid <> pg_backend_pid()"
                        )
                    )
                ).all()
                pytest.fail(
                    f"expected {count} row-lock waiter(s) on assignment_claims, "
                    f"saw {blocked} within {timeout}s; backends: {rows!r}"
                )
            await asyncio.sleep(0.05)


async def _assignment_status(
    factory: async_sessionmaker[AsyncSession], assignment_id: UUID
) -> AssignmentAvailability:
    async with factory() as session:
        row = await session.get(Assignment, assignment_id)
        assert row is not None
        return AssignmentAvailability(row.availability_status)


async def _claim_status(
    factory: async_sessionmaker[AsyncSession], claim_id: UUID
) -> AssignmentClaim:
    async with factory() as session:
        row = await session.get(AssignmentClaim, claim_id)
        assert row is not None
        return row


# --- happy path: inclusive boundary, provisional lock untouched ---------------------


@pytest.mark.integration
async def test_expire_at_inclusive_grace_boundary_leaves_reward_state(
    db_engine: AsyncEngine,
) -> None:
    """The expiry fires at exactly now == grace_deadline_at (NOT_DUE is
    strictly earlier), flips the assignment back to AVAILABLE, writes
    terminal_at == now, and emits exactly one CLAIM_EXPIRED event. A
    PROVISIONAL reward lock and every claim-time snapshot survive
    untouched: expiry pays nothing (spec §11.4) — reversals own the
    ledger."""
    factory = _factory(db_engine)
    events = InMemoryEventCollector()
    service = _service(events=events)
    run = uuid4().hex[:8]

    seeded = await _seed_claims(
        factory,
        run=run,
        specs=[
            ClaimSpec(
                status=ClaimStatus.CLAIMED,
                grace_at=_NOW,  # exactly at the boundary -> due
                reward_lock_status=RewardLockStatus.PROVISIONAL,
                reward_tier_locked=80,
                reward_locked_at=_NOW - timedelta(hours=2),
                locked_reward_points=80,
                latest_submission_id=uuid4(),
            )
        ],
    )
    task_ids = [task.id for task in seeded.tasks]
    user_ids = [seeded.teacher.id, seeded.student.id]
    try:
        async with factory() as session:
            result = await service.expire_claim_if_due(
                session, seeded.claims[0].id, _NOW
            )

        assert result.outcome is ExpiryOutcome.EXPIRED
        assert result.status is ClaimStatus.EXPIRED
        assert result.terminal_at == _NOW
        assert result.assignment_id == seeded.assignments[0].id
        assert result.task_id == seeded.tasks[0].id
        assert result.user_id == seeded.student.id

        loaded = await _claim_status(factory, seeded.claims[0].id)
        assert ClaimStatus(loaded.status) is ClaimStatus.EXPIRED
        assert loaded.terminal_at == _NOW
        assert (
            await _assignment_status(factory, seeded.assignments[0].id)
            is AssignmentAvailability.AVAILABLE
        )

        # No payout, no reward mutation: the reversal flow owns these.
        lock_status = RewardLockStatus(loaded.reward_lock_status)
        assert lock_status is RewardLockStatus.PROVISIONAL
        assert loaded.reward_tier_locked == 80
        assert loaded.reward_locked_at == _NOW - timedelta(hours=2)
        assert loaded.locked_reward_points == 80
        assert loaded.latest_submission_id is not None
        # Claim-time snapshots survive the terminal transition untouched.
        assert loaded.base_reward_points_snapshot == 100
        assert loaded.submission_schema_version == 1
        assert loaded.deadline_at == _NOW - timedelta(minutes=1440)
        assert loaded.grace_deadline_at == _NOW
        assert loaded.claimed_at == _NOW - timedelta(days=5)

        assert [event.event_type for event in events.events] == [CLAIM_EXPIRED]
        event = events.of_type(CLAIM_EXPIRED)[0]
        assert event.aggregate_type == "AssignmentClaim"
        assert event.aggregate_id == seeded.claims[0].id
        assert event.occurred_at == _NOW
        assert event.payload == {
            "user_id": str(seeded.student.id),
            "assignment_id": str(seeded.assignments[0].id),
            "task_id": str(seeded.tasks[0].id),
            "terminal_at": _NOW.isoformat(),
        }
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- effective deadline: revision extension, max semantics, future grace ------------


@pytest.mark.integration
async def test_revision_extension_defers_expiry_until_extended_deadline(
    db_engine: AsyncEngine,
) -> None:
    """The effective deadline is max(grace, revision when present): a
    review extension defers expiry (NOT_DUE before, EXPIRED at the
    inclusive revision instant); an earlier revision deadline never
    shortens grace; a plain future grace is not due. Nothing is written
    on a NOT_DUE decision."""
    factory = _factory(db_engine)
    events = InMemoryEventCollector()
    service = _service(events=events)
    run = uuid4().hex[:8]

    seeded = await _seed_claims(
        factory,
        run=run,
        specs=[
            # A: grace past, revision extends past the first probe instant.
            ClaimSpec(
                status=ClaimStatus.REVISION_REQUIRED,
                grace_at=_NOW - timedelta(hours=2),
                revision_at=_NOW + timedelta(hours=1),
            ),
            # B: revision EARLIER than grace — the max still equals grace.
            ClaimSpec(
                status=ClaimStatus.CLAIMED,
                grace_at=_NOW + timedelta(minutes=30),
                revision_at=_NOW - timedelta(hours=5),
            ),
            # C: plain future grace.
            ClaimSpec(
                status=ClaimStatus.CLAIMED,
                grace_at=_NOW + timedelta(hours=3),
            ),
        ],
    )
    task_ids = [task.id for task in seeded.tasks]
    user_ids = [seeded.teacher.id, seeded.student.id]
    try:
        loaded_b = await _claim_status(factory, seeded.claims[1].id)
        assert effective_expiry_deadline(loaded_b) == _NOW + timedelta(minutes=30)

        async with factory() as session:
            for index in range(3):
                result = await service.expire_claim_if_due(
                    session, seeded.claims[index].id, _NOW
                )
                assert result.outcome is ExpiryOutcome.NOT_DUE

        for index in range(3):
            loaded = await _claim_status(factory, seeded.claims[index].id)
            assert loaded.terminal_at is None
            assert (
                await _assignment_status(factory, seeded.assignments[index].id)
                is AssignmentAvailability.OCCUPIED
            )
        assert events.events == []

        # The revision instant itself is due (inclusive boundary), and a
        # REVISION_REQUIRED claim is actionable for expiry.
        async with factory() as session:
            result = await service.expire_claim_if_due(
                session, seeded.claims[0].id, _NOW + timedelta(hours=1)
            )
        assert result.outcome is ExpiryOutcome.EXPIRED
        assert result.terminal_at == _NOW + timedelta(hours=1)
        assert len(events.of_type(CLAIM_EXPIRED)) == 1
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


@pytest.mark.integration
async def test_naive_now_is_rejected_before_anything_happens(
    db_engine: AsyncEngine,
) -> None:
    """A naive ``now`` has no instant to compare against the deadline
    columns; the guard fires as ValueError before the claim-row lock is
    taken (same aware-only boundary as deadlines.py /
    business_day_window)."""
    factory = _factory(db_engine)
    service = _service()
    run = uuid4().hex[:8]

    seeded = await _seed_claims(
        factory,
        run=run,
        specs=[ClaimSpec(status=ClaimStatus.CLAIMED, grace_at=_NOW)],
    )
    task_ids = [task.id for task in seeded.tasks]
    user_ids = [seeded.teacher.id, seeded.student.id]
    try:
        async with factory() as session:
            with pytest.raises(ValueError, match="timezone-aware"):
                await service.expire_claim_if_due(
                    session, seeded.claims[0].id, datetime(2026, 1, 15, 9, 0)
                )
        loaded = await _claim_status(factory, seeded.claims[0].id)
        assert ClaimStatus(loaded.status) is ClaimStatus.CLAIMED
        assert loaded.terminal_at is None
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- candidate scan: matrix, ordering, limit ----------------------------------------


@pytest.mark.integration
async def test_scan_collects_due_actionable_claims_in_grace_order(
    db_engine: AsyncEngine,
) -> None:
    """The scan feeds the per-ID jobs: actionable statuses only, both
    deadline columns due (revision NULL-or-past), ordered by grace
    deadline ascending, bounded by the limit. Every non-candidate shape —
    mid-review, terminal, future grace, revision-extended — drops out."""
    factory = _factory(db_engine)
    run = uuid4().hex[:8]

    seeded = await _seed_claims(
        factory,
        run=run,
        specs=[
            # Included, in grace order:
            ClaimSpec(
                status=ClaimStatus.CLAIMED,
                grace_at=_NOW - timedelta(hours=3),
            ),
            ClaimSpec(
                status=ClaimStatus.REVISION_REQUIRED,
                grace_at=_NOW - timedelta(hours=2),
                revision_at=_NOW - timedelta(minutes=90),
            ),
            ClaimSpec(
                status=ClaimStatus.CLAIMED,
                grace_at=_NOW - timedelta(hours=1),
                revision_at=_NOW - timedelta(hours=4),  # max() == grace
            ),
            # Excluded — status not actionable:
            ClaimSpec(
                status=ClaimStatus.VALIDATING,
                grace_at=_NOW - timedelta(hours=3),
            ),
            ClaimSpec(
                status=ClaimStatus.UNDER_REVIEW,
                grace_at=_NOW - timedelta(hours=3),
            ),
            # Excluded — terminal:
            ClaimSpec(
                status=ClaimStatus.COMPLETED,
                grace_at=_NOW - timedelta(hours=3),
                terminal_at=_NOW - timedelta(hours=2),
            ),
            ClaimSpec(
                status=ClaimStatus.ABANDONED,
                grace_at=_NOW - timedelta(hours=3),
                terminal_at=_NOW - timedelta(hours=2),
            ),
            ClaimSpec(
                status=ClaimStatus.EXPIRED,
                grace_at=_NOW - timedelta(hours=3),
                terminal_at=_NOW - timedelta(hours=2),
            ),
            # Excluded — not due yet:
            ClaimSpec(
                status=ClaimStatus.CLAIMED,
                grace_at=_NOW + timedelta(hours=1),
            ),
            ClaimSpec(
                status=ClaimStatus.CLAIMED,
                grace_at=_NOW - timedelta(minutes=30),
                revision_at=_NOW + timedelta(minutes=30),
            ),
        ],
    )
    task_ids = [task.id for task in seeded.tasks]
    user_ids = [seeded.teacher.id, seeded.student.id]
    try:
        async with factory() as session:
            due = list(await collect_due_claim_ids(session, _NOW))
        assert due == [
            seeded.claims[0].id,
            seeded.claims[1].id,
            seeded.claims[2].id,
        ]

        async with factory() as session:
            first_two = list(await collect_due_claim_ids(session, _NOW, limit=2))
        assert first_two == [seeded.claims[0].id, seeded.claims[1].id]
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- protection: mid-review claims ---------------------------------------------------


@pytest.mark.integration
async def test_mid_review_claims_are_protected_from_expiry(
    db_engine: AsyncEngine,
) -> None:
    """A claim whose file is already in the review pipeline
    (VALIDATING/UNDER_REVIEW) after grace is PROTECTED: the review flow
    owns it, the expiry leaves status, terminal_at, and the OCCUPIED
    assignment untouched, and emits no event."""
    factory = _factory(db_engine)
    events = InMemoryEventCollector()
    service = _service(events=events)
    run = uuid4().hex[:8]

    seeded = await _seed_claims(
        factory,
        run=run,
        specs=[
            ClaimSpec(
                status=ClaimStatus.UNDER_REVIEW,
                grace_at=_NOW - timedelta(hours=1),
            ),
            ClaimSpec(
                status=ClaimStatus.VALIDATING,
                grace_at=_NOW - timedelta(hours=1),
            ),
        ],
    )
    task_ids = [task.id for task in seeded.tasks]
    user_ids = [seeded.teacher.id, seeded.student.id]
    try:
        for index in range(2):
            async with factory() as session:
                result = await service.expire_claim_if_due(
                    session, seeded.claims[index].id, _NOW
                )
            assert result.outcome is ExpiryOutcome.PROTECTED
            assert result.terminal_at is None
            loaded = await _claim_status(factory, seeded.claims[index].id)
            assert loaded.status == seeded.claims[index].status
            assert loaded.terminal_at is None
            assert (
                await _assignment_status(factory, seeded.assignments[index].id)
                is AssignmentAvailability.OCCUPIED
            )
        assert events.events == []
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- amendment 2: only VALIDATED protects; the inspector is the seam ------------------


@pytest.mark.integration
async def test_inspector_true_protects_actionable_due_claim(
    db_engine: AsyncEngine,
) -> None:
    """A due, actionable claim whose submission the inspector reports as
    valid is VALID_SUBMISSION: nothing moves and no event fires. The
    inspector is consulted with the session and the exact claim id (it
    runs INSIDE the locked transaction — pinned structurally by the
    expiry-first race below)."""
    factory = _factory(db_engine)
    events = InMemoryEventCollector()
    run = uuid4().hex[:8]

    seeded = await _seed_claims(
        factory,
        run=run,
        specs=[
            ClaimSpec(
                status=ClaimStatus.CLAIMED,
                grace_at=_NOW - timedelta(hours=1),
            ),
        ],
    )
    inspector = RecordingInspector(answers={seeded.claims[0].id: True})
    service = _service(events=events, inspector=inspector)
    task_ids = [task.id for task in seeded.tasks]
    user_ids = [seeded.teacher.id, seeded.student.id]
    try:
        async with factory() as session:
            result = await service.expire_claim_if_due(
                session, seeded.claims[0].id, _NOW
            )
        assert result.outcome is ExpiryOutcome.VALID_SUBMISSION
        assert inspector.calls == [seeded.claims[0].id]
        loaded = await _claim_status(factory, seeded.claims[0].id)
        assert ClaimStatus(loaded.status) is ClaimStatus.CLAIMED
        assert loaded.terminal_at is None
        assert (
            await _assignment_status(factory, seeded.assignments[0].id)
            is AssignmentAvailability.OCCUPIED
        )
        assert events.events == []
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


@pytest.mark.integration
async def test_strict_reading_unvalidated_submission_still_expires(
    db_engine: AsyncEngine,
) -> None:
    """Plan-04 amendment 2, strict reading: only a machine-VALIDATED
    submission protects. With the default inspector — and explicitly with
    NoValidSubmissionsInspector — a due CLAIM carrying an in-window but
    un-validated submission (latest_submission_id set) still expires; the
    merged branch swaps the real VALIDATED reader in at the same seam."""
    factory = _factory(db_engine)
    events = InMemoryEventCollector()
    default_service = _service(events=events)  # no inspector argument
    explicit_service = ClaimService(
        clock=FrozenClock(_NOW),
        event_publisher=InMemoryEventCollector(),
        valid_submission_inspector=NoValidSubmissionsInspector(),
    )
    run = uuid4().hex[:8]

    seeded = await _seed_claims(
        factory,
        run=run,
        specs=[
            ClaimSpec(
                status=ClaimStatus.CLAIMED,
                grace_at=_NOW - timedelta(hours=1),
                latest_submission_id=uuid4(),
            ),
            ClaimSpec(
                status=ClaimStatus.CLAIMED,
                grace_at=_NOW - timedelta(hours=1),
                latest_submission_id=uuid4(),
            ),
        ],
    )
    task_ids = [task.id for task in seeded.tasks]
    user_ids = [seeded.teacher.id, seeded.student.id]
    try:
        # The seam's default answers False (no submissions table on this
        # branch; the merge wires the VALIDATED reader).
        async with factory() as session:
            assert not await NoValidSubmissionsInspector().has_valid_submission(
                session, seeded.claims[0].id
            )

        async with factory() as session:
            first = await default_service.expire_claim_if_due(
                session, seeded.claims[0].id, _NOW
            )
        assert first.outcome is ExpiryOutcome.EXPIRED

        async with factory() as session:
            second = await explicit_service.expire_claim_if_due(
                session, seeded.claims[1].id, _NOW
            )
        assert second.outcome is ExpiryOutcome.EXPIRED
        assert len(events.of_type(CLAIM_EXPIRED)) == 1
        for index in range(2):
            assert (
                await _assignment_status(factory, seeded.assignments[index].id)
                is AssignmentAvailability.AVAILABLE
            )
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- idempotency and sticky assignment statuses ---------------------------------------


@pytest.mark.integration
async def test_terminal_claims_are_idempotent_noops(
    db_engine: AsyncEngine,
) -> None:
    """Replaying expiry against COMPLETED/ABANDONED/EXPIRED claims answers
    ALREADY_TERMINAL: no second event, no terminal_at rewrite, no release
    flip (the EXPIRED seed's assignment is already AVAILABLE and a
    COMPLETED seed's stays COMPLETED)."""
    factory = _factory(db_engine)
    events = InMemoryEventCollector()
    service = _service(events=events)
    run = uuid4().hex[:8]

    seeded = await _seed_claims(
        factory,
        run=run,
        specs=[
            ClaimSpec(
                status=ClaimStatus.COMPLETED,
                grace_at=_NOW - timedelta(hours=3),
                terminal_at=_NOW - timedelta(hours=2),
                assignment_status=AssignmentAvailability.COMPLETED,
            ),
            ClaimSpec(
                status=ClaimStatus.ABANDONED,
                grace_at=_NOW - timedelta(hours=3),
                terminal_at=_NOW - timedelta(hours=2),
                assignment_status=AssignmentAvailability.AVAILABLE,
            ),
            ClaimSpec(
                status=ClaimStatus.EXPIRED,
                grace_at=_NOW - timedelta(hours=3),
                terminal_at=_NOW - timedelta(hours=2),
                assignment_status=AssignmentAvailability.AVAILABLE,
            ),
        ],
    )
    task_ids = [task.id for task in seeded.tasks]
    user_ids = [seeded.teacher.id, seeded.student.id]
    try:
        for index in range(3):
            async with factory() as session:
                result = await service.expire_claim_if_due(
                    session, seeded.claims[index].id, _NOW
                )
            assert result.outcome is ExpiryOutcome.ALREADY_TERMINAL
            assert result.terminal_at == _NOW - timedelta(hours=2)
            loaded = await _claim_status(factory, seeded.claims[index].id)
            assert loaded.status == seeded.claims[index].status
            assert loaded.terminal_at == _NOW - timedelta(hours=2)
        assert events.events == []
        assert (
            await _assignment_status(factory, seeded.assignments[0].id)
            is AssignmentAvailability.COMPLETED
        )
        assert (
            await _assignment_status(factory, seeded.assignments[1].id)
            is AssignmentAvailability.AVAILABLE
        )
        assert (
            await _assignment_status(factory, seeded.assignments[2].id)
            is AssignmentAvailability.AVAILABLE
        )
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


@pytest.mark.integration
async def test_sticky_assignment_statuses_survive_expiry(
    db_engine: AsyncEngine,
) -> None:
    """The release flips OCCUPIED -> AVAILABLE only: a RETIRED or
    COMPLETED assignment is sticky (spec §8.2) and never resurrected,
    while the claim still reaches EXPIRED so the quota and the
    reassignment exclusion apply regardless."""
    factory = _factory(db_engine)
    events = InMemoryEventCollector()
    service = _service(events=events)
    run = uuid4().hex[:8]

    specs = [
        ClaimSpec(
            status=ClaimStatus.CLAIMED,
            grace_at=_NOW - timedelta(hours=1),
            assignment_status=AssignmentAvailability.RETIRED,
        ),
        ClaimSpec(
            status=ClaimStatus.CLAIMED,
            grace_at=_NOW - timedelta(hours=1),
            assignment_status=AssignmentAvailability.COMPLETED,
        ),
    ]
    seeded = await _seed_claims(factory, run=run, specs=specs)
    task_ids = [task.id for task in seeded.tasks]
    user_ids = [seeded.teacher.id, seeded.student.id]
    try:
        for index in range(2):
            async with factory() as session:
                result = await service.expire_claim_if_due(
                    session, seeded.claims[index].id, _NOW
                )
            assert result.outcome is ExpiryOutcome.EXPIRED
            assert (
                await _assignment_status(factory, seeded.assignments[index].id)
                is specs[index].assignment_status
            )
        assert len(events.of_type(CLAIM_EXPIRED)) == 2
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


@pytest.mark.integration
async def test_missing_claim_reports_missing(db_engine: AsyncEngine) -> None:
    """An unknown claim id answers MISSING (the scan and the per-ID job
    tolerate at-least-once redelivery against rows removed by ops)."""
    factory = _factory(db_engine)
    service = _service()
    async with factory() as session:
        result = await service.expire_claim_if_due(session, uuid4(), _NOW)
    assert result.outcome is ExpiryOutcome.MISSING
    assert result.status is None
    assert result.terminal_at is None


# --- race 1: finalize commits first, expiry waits and re-judges -----------------------


@pytest.mark.integration
async def test_finalize_first_expiry_waits_and_rejudges(db_engine: AsyncEngine) -> None:
    """A submission finalize that holds the claim-row lock and commits
    BEFORE the expiry's lock lands is always observed, because every
    expiry decision is made on the locked row AFTER acquiring it — the
    pg_stat_activity poll proves the expiry truly parked on the lock
    first. Status variant: the finalize flips the claim UNDER_REVIEW and
    the expiry answers PROTECTED. Inspector variant: the finalize leaves
    the status actionable but marks the submission valid (the flag the
    merged VALIDATED reader would answer True for); the expiry answers
    VALID_SUBMISSION. In both variants the assignment never returns to
    AVAILABLE and no event fires."""
    factory = _factory(db_engine)
    events = InMemoryEventCollector()
    run = uuid4().hex[:8]

    seeded = await _seed_claims(
        factory,
        run=run,
        specs=[
            # Variant A: finalize flips the status under the lock.
            ClaimSpec(
                status=ClaimStatus.CLAIMED,
                grace_at=_NOW - timedelta(hours=1),
            ),
            # Variant B: finalize only marks the submission valid.
            ClaimSpec(
                status=ClaimStatus.CLAIMED,
                grace_at=_NOW - timedelta(hours=1),
            ),
        ],
    )
    task_ids = [task.id for task in seeded.tasks]
    user_ids = [seeded.teacher.id, seeded.student.id]
    try:
        # Variant A — status protection observed after lock-wait.
        service = _service(events=events)
        async with factory() as holder:
            claim_a = await holder.scalar(
                select(AssignmentClaim)
                .where(AssignmentClaim.id == seeded.claims[0].id)
                .with_for_update()
            )
            assert claim_a is not None
            expiry_task = asyncio.create_task(
                _expire_one(service, factory, seeded.claims[0].id, _NOW)
            )
            await _await_row_lock_waiter(factory)
            claim_a.status = ClaimStatus.UNDER_REVIEW
            await holder.commit()
        protected = await expiry_task
        assert protected.outcome is ExpiryOutcome.PROTECTED
        loaded = await _claim_status(factory, seeded.claims[0].id)
        assert ClaimStatus(loaded.status) is ClaimStatus.UNDER_REVIEW
        assert loaded.terminal_at is None
        assert (
            await _assignment_status(factory, seeded.assignments[0].id)
            is AssignmentAvailability.OCCUPIED
        )

        # Variant B — inspector protection observed after lock-wait: the
        # flag flips while the finalize still holds the claim-row lock,
        # so an expiry that consulted the inspector BEFORE locking would
        # have read False and expired.
        inspector = RecordingInspector()
        service_b = _service(events=events, inspector=inspector)
        async with factory() as holder:
            claim_b = await holder.scalar(
                select(AssignmentClaim)
                .where(AssignmentClaim.id == seeded.claims[1].id)
                .with_for_update()
            )
            assert claim_b is not None
            expiry_task = asyncio.create_task(
                _expire_one(service_b, factory, seeded.claims[1].id, _NOW)
            )
            await _await_row_lock_waiter(factory)
            inspector.answers[seeded.claims[1].id] = True
            await holder.commit()
        valid_submission = await expiry_task
        assert valid_submission.outcome is ExpiryOutcome.VALID_SUBMISSION
        assert inspector.calls == [seeded.claims[1].id]
        loaded = await _claim_status(factory, seeded.claims[1].id)
        assert ClaimStatus(loaded.status) is ClaimStatus.CLAIMED
        assert loaded.terminal_at is None
        assert (
            await _assignment_status(factory, seeded.assignments[1].id)
            is AssignmentAvailability.OCCUPIED
        )
        assert events.events == []
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- race 2: expiry holds the lock; the finalize aborts ------------------------------


@pytest.mark.integration
async def test_expiry_first_finalize_blocks_then_aborts(
    db_engine: AsyncEngine,
) -> None:
    """The expiry wins the claim-row lock and parks INSIDE the inspector
    (the ParkingInspector's entered event fires only after the FOR UPDATE
    — proving the inspector is the in-locked-transaction seam). A finalize
    arriving now BLOCKS on the claim-row lock (pg_stat_activity proof);
    when the expiry commits EXPIRED and releases the assignment, the
    finalize re-judges the locked row, aborts, and its UNDER_REVIEW flip
    never runs: the submission can no longer own an EXPIRED claim."""
    factory = _factory(db_engine)
    events = InMemoryEventCollector()
    inspector = ParkingInspector()
    service = _service(events=events, inspector=inspector)
    run = uuid4().hex[:8]

    seeded = await _seed_claims(
        factory,
        run=run,
        specs=[
            ClaimSpec(
                status=ClaimStatus.CLAIMED,
                grace_at=_NOW - timedelta(hours=1),
            ),
        ],
    )
    task_ids = [task.id for task in seeded.tasks]
    user_ids = [seeded.teacher.id, seeded.student.id]
    try:
        expiry_task = asyncio.create_task(
            _expire_one(service, factory, seeded.claims[0].id, _NOW)
        )
        await asyncio.wait_for(inspector.entered.wait(), timeout=5)
        assert inspector.calls == [seeded.claims[0].id]

        finalize_task = asyncio.create_task(
            _guarded_finalize(factory, seeded.claims[0].id)
        )
        await _await_row_lock_waiter(factory)

        inspector.release.set()
        expired = await expiry_task
        finalize = await finalize_task

        assert expired.outcome is ExpiryOutcome.EXPIRED
        assert expired.terminal_at == _NOW
        assert finalize.aborted
        assert finalize.observed_status is ClaimStatus.EXPIRED
        assert finalize.flipped_to is None

        loaded = await _claim_status(factory, seeded.claims[0].id)
        assert ClaimStatus(loaded.status) is ClaimStatus.EXPIRED
        assert loaded.terminal_at == _NOW
        assert (
            await _assignment_status(factory, seeded.assignments[0].id)
            is AssignmentAvailability.AVAILABLE
        )
        assert len(events.of_type(CLAIM_EXPIRED)) == 1
    finally:
        inspector.release.set()
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- race 3: three simultaneous expiries ----------------------------------------------


@pytest.mark.integration
async def test_simultaneous_expiries_land_exactly_once(
    db_engine: AsyncEngine,
) -> None:
    """Three simultaneous expiry attempts on one due claim (barrier
    release; the two losers proven parked on the row lock via
    pg_stat_activity while the winner sits in the inspector): exactly one
    EXPIRED plus two ALREADY_TERMINAL replays, one audit event, one
    release, one terminal_at — in any interleaving (legal-outcome
    disjunction)."""
    factory = _factory(db_engine)
    events = InMemoryEventCollector()
    inspector = ParkingInspector()
    service = _service(events=events, inspector=inspector)
    run = uuid4().hex[:8]

    seeded = await _seed_claims(
        factory,
        run=run,
        specs=[
            ClaimSpec(
                status=ClaimStatus.CLAIMED,
                grace_at=_NOW - timedelta(hours=1),
            ),
        ],
    )
    task_ids = [task.id for task in seeded.tasks]
    user_ids = [seeded.teacher.id, seeded.student.id]
    try:
        start = asyncio.Event()
        racers = [
            asyncio.create_task(
                _expire_one(service, factory, seeded.claims[0].id, _NOW, start)
            )
            for _ in range(3)
        ]
        await asyncio.sleep(0.05)  # park every racer on the barrier
        start.set()
        await asyncio.wait_for(inspector.entered.wait(), timeout=5)
        await _await_row_lock_waiter(factory, count=2)
        inspector.release.set()
        outcomes = await asyncio.gather(*racers)

        counts: dict[ExpiryOutcome, int] = {}
        for result in outcomes:
            counts[result.outcome] = counts.get(result.outcome, 0) + 1
        assert counts == {
            ExpiryOutcome.EXPIRED: 1,
            ExpiryOutcome.ALREADY_TERMINAL: 2,
        }
        # Only the winner ever consulted the inspector: the losers judged
        # ALREADY_TERMINAL on the locked row before reaching the seam.
        assert inspector.calls == [seeded.claims[0].id]

        loaded = await _claim_status(factory, seeded.claims[0].id)
        assert ClaimStatus(loaded.status) is ClaimStatus.EXPIRED
        assert loaded.terminal_at == _NOW
        assert (
            await _assignment_status(factory, seeded.assignments[0].id)
            is AssignmentAvailability.AVAILABLE
        )
        assert len(events.of_type(CLAIM_EXPIRED)) == 1
    finally:
        inspector.release.set()
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)
