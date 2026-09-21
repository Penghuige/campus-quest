# backend/tests/workers/test_cleanup_deletion_claim.py
"""The pass-4b deletion-claim contract against real PostgreSQL (spec
§13/§27; G15: concurrency invariants are proven on real PG with
independent connections, never with mocks).

What the owner-named TOCTOU race proved BEFORE pass 4b: the cleanup scan
returned detached boolean snapshots, so a protection (legal_hold, a
claim entering the review pipeline) that landed between the scan and
the provider delete was invisible — the object died anyway. The claim
conditional UPDATE closes it: every guard is re-evaluated against
CURRENT committed state in the same statement that takes the claim.

Coverage:

- the claim predicate re-evaluates EVERY guard (legal hold, review
  statuses, retention due, permanent, already deleted, exclusivity) on
  the live row, not the scan snapshot;
- the owner-named race, two independent connections: A scans, B commits
  a protection, A's claim FAILS and the object survives;
- the protection side of the ruling: while a claim is held, the guard
  helper raises the typed 409; after release (the provider-failure
  path) or completion (deleted_at set) the protection passes again;
- the REAL protected write points: validation tx1's VALIDATING entry
  and the reward-lock UNDER_REVIEW entry refuse with the typed 409 and
  roll back whole;
- the orphan-intent cleanup matrix: expired-open and burned intents
  are claimed and deleted, finalized intents are never touched, a
  missing object is idempotent success, a provider failure releases
  the claim for the next scan.

Harness notes (the test_file_cleanup.py conventions): explicit
committed sessions from a NullPool engine factory — every asyncio.run
phase runs on a fresh event loop, so pooled connections must never be
reused across them; usernames embed a per-run token; every test removes
its rows with committed DELETEs in FK order in ``finally``.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.modules.files.cleanup_service import (
    FileCleanupOutcome,
    FileRecord,
    OrphanIntentOutcome,
    OrphanIntentSummary,
    cleanup_expired_file,
    cleanup_orphaned_intents,
)
from tests.fakes.integrations import FakeObjectStorage

NOW = datetime(2026, 9, 22, 3, 0, tzinfo=UTC)
TTL = timedelta(minutes=5)

_TEST_DATABASE_MARKER = "campusquest_test"
_DEFAULT_DATABASE_URL = (
    "postgresql+asyncpg://test:test@localhost:15434/campusquest_test_3"
)

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)


def _database_url() -> str:
    """The integration database URL, refusing non-test databases."""
    import os

    from sqlalchemy.engine import make_url

    url = os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)
    database = make_url(url).database or ""
    if _TEST_DATABASE_MARKER not in database:
        pytest.fail(
            f"Refusing deletion-claim tests against non-test database "
            f"{database!r} (DATABASE_URL={url!r}): the database name must "
            f"contain {_TEST_DATABASE_MARKER!r}."
        )
    return url


def _factory() -> async_sessionmaker[AsyncSession]:
    """NullPool session factory: fresh connection per checkout, so
    asyncio.run phases on fresh loops never share a pooled connection."""
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False)


@dataclass(slots=True)
class World:
    """One seeded world: teacher, student, task, one assignment+claim,
    and the submissions/intents a scenario asked for."""

    claim_id: UUID
    submission_ids: dict[str, UUID]
    intent_ids: dict[str, UUID]
    task_ids: list[UUID]
    user_ids: list[UUID]


@dataclass(frozen=True, slots=True)
class SubmissionSpec:
    """One direct-seeded submission row under the world's claim."""

    name: str
    version: int
    retention_until: datetime | None
    validation_status: str = "VALIDATED"
    cleanup_claimed_at: datetime | None = None
    deleted_at: datetime | None = None
    mark_latest: bool = False


@dataclass(frozen=True, slots=True)
class IntentSpec:
    """One direct-seeded upload-intent row under the world's claim."""

    name: str
    expires_at: datetime
    consumed_at: datetime | None = None
    finalized_version: int | None = None


async def _seed_world(
    maker: async_sessionmaker[AsyncSession],
    run: str,
    *,
    claim_status: str = "CLAIMED",
    submissions: tuple[SubmissionSpec, ...] = (),
    intents: tuple[IntentSpec, ...] = (),
) -> World:
    """Seed one teacher/student/task/assignment/claim plus the
    submission and intent shapes the scenario needs. Claim status,
    cleanup claims, and deletions are direct-seeded states — this file
    judges the claim PREDICATE and the guards, not the flows that
    produce those states (the flow tests live in the submissions
    suites)."""
    from app.modules.identity.enums import Role, UserStatus
    from app.modules.identity.models import User
    from app.modules.submissions.models import Submission, UploadIntent
    from app.modules.tasks.enums import (
        AssignmentAvailability,
        DeadlineMode,
        RewardLockStatus,
        TaskRarity,
        TaskStatus,
        TaskType,
    )
    from app.modules.tasks.models import Assignment, AssignmentClaim, Task

    async with maker() as session:
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
            title="期末课程问卷数据采集",
            description="采集问卷数据。",
            task_type=TaskType.DATA_CRAWL,
            rarity=TaskRarity.NORMAL,
            base_reward_points=100,
            status=TaskStatus.PUBLISHED,
            deadline_mode=DeadlineMode.RELATIVE,
            duration_minutes=4320,
            submission_schema={"columns": [{"name": "note", "type": "string"}]},
            submission_schema_version=1,
            allowed_file_types=["CSV"],
            max_file_size_bytes=10 * 1024 * 1024,
            notification_channels=["SMS"],
        )
        session.add(task)
        await session.flush()
        assignment = Assignment(
            task_id=task.id,
            platform="xiaohongshu",
            keyword=f"问卷{run[-4:]}",
            availability_status=AssignmentAvailability.OCCUPIED,
        )
        session.add(assignment)
        await session.flush()
        claim = AssignmentClaim(
            assignment_id=assignment.id,
            task_id=task.id,
            user_id=student.id,
            status=claim_status,
            claimed_at=NOW - timedelta(days=400),
            deadline_at=NOW - timedelta(days=200),
            grace_deadline_at=NOW - timedelta(days=199),
            reward_policy_snapshot={"version": 1},
            base_reward_points_snapshot=100,
            submission_schema_version=1,
            reward_lock_status=RewardLockStatus.NONE,
        )
        session.add(claim)
        await session.flush()

        submission_ids: dict[str, UUID] = {}
        for spec in submissions:
            submission = Submission(
                claim_id=claim.id,
                version=spec.version,
                object_key=f"submissions/{claim.id}/{spec.version}-{uuid.uuid4()}",
                original_filename="数据.csv",
                declared_type="CSV",
                file_size=128,
                submitted_at=NOW - timedelta(days=365),
                validation_status=spec.validation_status,
                retention_until=spec.retention_until,
                retention_permanent=spec.retention_until is None,
                cleanup_claimed_at=spec.cleanup_claimed_at,
                deleted_at=spec.deleted_at,
            )
            session.add(submission)
            await session.flush()
            submission_ids[spec.name] = submission.id
            if spec.mark_latest:
                claim.latest_submission_id = submission.id

        intent_ids: dict[str, UUID] = {}
        for spec in intents:
            intent = UploadIntent(
                claim_id=claim.id,
                object_key=f"submissions/{claim.id}/intent-{spec.name}-{uuid.uuid4()}",
                filename="数据.csv",
                declared_type="CSV",
                declared_size=128,
                created_at=spec.expires_at - timedelta(minutes=15),
                expires_at=spec.expires_at,
                consumed_at=spec.consumed_at,
                finalized_submission_id=(
                    submission_ids_by_version(
                        submissions, submission_ids, spec.finalized_version
                    )
                    if spec.finalized_version is not None
                    else None
                ),
            )
            session.add(intent)
            await session.flush()
            intent_ids[spec.name] = intent.id

        await session.commit()
        return World(
            claim_id=claim.id,
            submission_ids=submission_ids,
            intent_ids=intent_ids,
            task_ids=[task.id],
            user_ids=[teacher.id, student.id],
        )


def submission_ids_by_version(
    submissions: tuple[SubmissionSpec, ...],
    submission_ids: dict[str, UUID],
    version: int,
) -> UUID:
    for spec in submissions:
        if spec.version == version:
            return submission_ids[spec.name]
    raise AssertionError(f"no seeded submission with version {version}")


async def _cleanup_world(maker: async_sessionmaker[AsyncSession], world: World) -> None:
    """Explicit committed cleanup in FK order (intents -> submissions ->
    claims -> assignments -> tasks -> users)."""
    from app.modules.identity.models import User
    from app.modules.submissions.models import (
        RewardLockHistory,
        Submission,
        SubmissionValidation,
        UploadIntent,
    )
    from app.modules.tasks.models import Assignment, AssignmentClaim, Task

    async with maker() as session:
        await session.execute(
            delete(UploadIntent).where(UploadIntent.claim_id == world.claim_id)
        )
        await session.execute(
            delete(SubmissionValidation).where(
                SubmissionValidation.submission_id.in_(
                    select(Submission.id).where(Submission.claim_id == world.claim_id)
                )
            )
        )
        await session.execute(
            delete(RewardLockHistory).where(
                RewardLockHistory.claim_id == world.claim_id
            )
        )
        await session.execute(
            delete(Submission).where(Submission.claim_id == world.claim_id)
        )
        await session.execute(
            delete(AssignmentClaim).where(AssignmentClaim.id == world.claim_id)
        )
        await session.execute(
            delete(Assignment).where(Assignment.task_id.in_(world.task_ids))
        )
        await session.execute(delete(Task).where(Task.id.in_(world.task_ids)))
        await session.execute(delete(User).where(User.id.in_(world.user_ids)))
        await session.commit()


def _scan_snapshot(maker: async_sessionmaker[AsyncSession], world: World) -> FileRecord:
    """Read the world's seeded submission row back as a FileRecord —
    the detached snapshot the scan would have handed the worker."""

    async def _read() -> FileRecord:
        from app.modules.submissions.models import Submission

        async with maker() as session:
            row = await session.get(Submission, world.submission_ids["due"])
            assert row is not None
            return FileRecord(
                submission_id=row.id,
                object_key=row.object_key,
                retention_until=row.retention_until,
            )

    return asyncio.run(_read())


# --- the claim predicate over real PostgreSQL -----------------------------------------


_DUE_SPEC = (
    SubmissionSpec(
        name="due",
        version=1,
        retention_until=NOW - timedelta(days=30),
        mark_latest=True,
    ),
)


@pytest.mark.integration
def test_real_repository_claim_reevaluates_every_guard() -> None:
    """The conditional claim fails on EVERY §13/§27 guard when judged
    against the live row — legal hold, the claim being inside the
    review pipeline (VALIDATING / UNDER_REVIEW), retention not yet due,
    permanent, already deleted — and on exclusivity: a second claim on
    a claimed row loses. Guards are judged per current committed state
    in ONE statement; each variant mutates the row directly (the states
    the flows produce) and re-claims."""
    from app.modules.submissions.models import Submission
    from app.modules.tasks.models import AssignmentClaim
    from app.workers.jobs.cleanup_files import SubmissionCleanupRepository

    maker = _factory()
    run = uuid.uuid4().hex[:8]
    world = asyncio.run(_seed_world(maker, run, submissions=_DUE_SPEC))
    repository = SubmissionCleanupRepository(maker)
    record: FileRecord | None = None

    async def _reset() -> None:
        async with maker() as session:
            await session.execute(
                update(Submission)
                .where(Submission.id == world.submission_ids["due"])
                .values(
                    legal_hold=False,
                    cleanup_claimed_at=None,
                    deleted_at=None,
                    retention_until=NOW - timedelta(days=30),
                    retention_permanent=False,
                )
            )
            await session.execute(
                update(AssignmentClaim)
                .where(AssignmentClaim.id == world.claim_id)
                .values(status="CLAIMED")
            )
            await session.commit()

    async def _guard_variant(mutate: Any, *, also_reset: bool = True) -> bool:
        assert record is not None  # set inside try before any use
        if also_reset:
            await _reset()
        async with maker() as session:
            await mutate(session)
            await session.commit()
        return await repository.claim_for_cleanup(record, now=NOW)

    async def _noop(session: AsyncSession) -> None:
        pass

    try:
        record = _scan_snapshot(maker, world)
        assert asyncio.run(_guard_variant(_noop)), (
            "an eligible row must be claimable (and claims the row)"
        )
        assert asyncio.run(repository.claim_for_cleanup(record, now=NOW)) is False, (
            "exclusivity: a second claim on the claimed row must fail"
        )

        async def _legal_hold(session: AsyncSession) -> None:
            await session.execute(
                update(Submission)
                .where(Submission.id == world.submission_ids["due"])
                .values(legal_hold=True)
            )

        assert asyncio.run(_guard_variant(_legal_hold)) is False

        for protected_status in ("VALIDATING", "UNDER_REVIEW"):

            async def _status(
                session: AsyncSession, status: str = protected_status
            ) -> None:
                await session.execute(
                    update(AssignmentClaim)
                    .where(AssignmentClaim.id == world.claim_id)
                    .values(status=status)
                )

            assert asyncio.run(_guard_variant(_status)) is False, (
                f"claim must fail while the claim row is {protected_status}"
            )

        async def _not_due(session: AsyncSession) -> None:
            await session.execute(
                update(Submission)
                .where(Submission.id == world.submission_ids["due"])
                .values(retention_until=NOW + timedelta(days=1))
            )

        assert asyncio.run(_guard_variant(_not_due)) is False

        async def _permanent(session: AsyncSession) -> None:
            await session.execute(
                update(Submission)
                .where(Submission.id == world.submission_ids["due"])
                .values(retention_permanent=True, retention_until=None)
            )

        assert asyncio.run(_guard_variant(_permanent)) is False

        async def _deleted(session: AsyncSession) -> None:
            await session.execute(
                update(Submission)
                .where(Submission.id == world.submission_ids["due"])
                .values(deleted_at=NOW - timedelta(minutes=1))
            )

        assert asyncio.run(_guard_variant(_deleted)) is False
    finally:
        asyncio.run(_cleanup_world(maker, world))


# --- the owner-named race: protection between scan and claim --------------------------


@pytest.mark.integration
def test_race_legal_hold_between_scan_and_claim_loses_the_deletion() -> None:
    """Owner-named TOCTOU race, real PG, independent connections: A's
    scan lists the due row; B (another connection) commits a legal_hold
    on it; A's claim must FAIL — the WHERE re-evaluates the live row,
    the provider delete never happens, and the object survives."""
    from app.modules.submissions.models import Submission
    from app.workers.jobs.cleanup_files import SubmissionCleanupRepository

    maker = _factory()
    run = uuid.uuid4().hex[:8]
    world = asyncio.run(_seed_world(maker, run, submissions=_DUE_SPEC))
    repository = SubmissionCleanupRepository(maker)

    try:
        # Connection A: the scan snapshot.
        record = _scan_snapshot(maker, world)

        # Connection B: the protection lands and COMMITS.
        async def _place_hold() -> None:
            async with maker() as b_connection:
                await b_connection.execute(
                    update(Submission)
                    .where(Submission.id == world.submission_ids["due"])
                    .values(legal_hold=True)
                )
                await b_connection.commit()

        asyncio.run(_place_hold())

        # Connection A: the claim re-evaluates every guard on the live
        # row and must lose.
        claimed = asyncio.run(repository.claim_for_cleanup(record, now=NOW))
        assert claimed is False

        storage = FakeObjectStorage()
        outcome = asyncio.run(
            cleanup_expired_file(record, repo=repository, storage=storage, now=NOW)
        )
        assert outcome is FileCleanupOutcome.SKIPPED_CLAIM_LOST
        assert storage.deleted_keys == []

        async def _state() -> tuple[Any, Any]:
            async with maker() as session:
                row = await session.get(Submission, world.submission_ids["due"])
                assert row is not None
                return row.cleanup_claimed_at, row.deleted_at

        claim_state, deleted_state = asyncio.run(_state())
        assert claim_state is None
        assert deleted_state is None
    finally:
        asyncio.run(_cleanup_world(maker, world))


@pytest.mark.integration
def test_race_review_entry_between_scan_and_claim_loses_the_deletion() -> None:
    """Owner-named race, VALIDATING variant: A scans the due row (its
    claim is actionable); B commits the claim's transition INTO the
    review pipeline; A's claim fails and the object survives."""
    from app.modules.tasks.models import AssignmentClaim
    from app.workers.jobs.cleanup_files import SubmissionCleanupRepository

    maker = _factory()
    run = uuid.uuid4().hex[:8]
    world = asyncio.run(_seed_world(maker, run, submissions=_DUE_SPEC))
    repository = SubmissionCleanupRepository(maker)

    try:
        record = _scan_snapshot(maker, world)

        async def _enter_review() -> None:
            async with maker() as b_connection:
                await b_connection.execute(
                    update(AssignmentClaim)
                    .where(AssignmentClaim.id == world.claim_id)
                    .values(status="VALIDATING")
                )
                await b_connection.commit()

        asyncio.run(_enter_review())

        claimed = asyncio.run(repository.claim_for_cleanup(record, now=NOW))
        assert claimed is False

        storage = FakeObjectStorage()
        outcome = asyncio.run(
            cleanup_expired_file(record, repo=repository, storage=storage, now=NOW)
        )
        assert outcome is FileCleanupOutcome.SKIPPED_CLAIM_LOST
        assert storage.deleted_keys == []
    finally:
        asyncio.run(_cleanup_world(maker, world))


# --- the protection side: typed 409 while a claim is held -----------------------------


@pytest.mark.integration
def test_held_claim_rejects_protection_then_release_admits_it_again() -> None:
    """While a submission holds an UNFINISHED claim (claimed, not yet
    deleted), the protection guard raises the typed 409 naming it; a
    release (the provider-failure path) lifts the rejection, and so
    does a COMPLETED deletion (deleted_at set — the object is gone,
    protection is moot)."""
    from app.modules.submissions.cleanup_claim import (
        CleanupClaimConflictError,
        ensure_no_active_cleanup_claim,
    )
    from app.modules.submissions.models import Submission
    from app.workers.jobs.cleanup_files import SubmissionCleanupRepository

    maker = _factory()
    run = uuid.uuid4().hex[:8]
    world = asyncio.run(_seed_world(maker, run, submissions=_DUE_SPEC))
    repository = SubmissionCleanupRepository(maker)

    async def _guard() -> None:
        async with maker() as session:
            await ensure_no_active_cleanup_claim(session, world.claim_id)

    try:
        record = _scan_snapshot(maker, world)
        assert asyncio.run(repository.claim_for_cleanup(record, now=NOW))

        with pytest.raises(CleanupClaimConflictError) as conflict:
            asyncio.run(_guard())
        assert conflict.value.status_code == 409
        assert conflict.value.details["submission_ids"] == [
            str(world.submission_ids["due"])
        ]

        asyncio.run(repository.release_cleanup_claim(record))
        asyncio.run(_guard())  # released: protection may proceed

        assert asyncio.run(repository.claim_for_cleanup(record, now=NOW))

        async def _complete_deletion() -> None:
            async with maker() as session:
                await session.execute(
                    update(Submission)
                    .where(Submission.id == world.submission_ids["due"])
                    .values(deleted_at=NOW)
                )
                await session.commit()

        asyncio.run(_complete_deletion())
        asyncio.run(_guard())  # completed: not a conflict anymore
    finally:
        asyncio.run(_cleanup_world(maker, world))


@pytest.mark.integration
def test_validation_start_rejects_while_cleanup_claim_held() -> None:
    """The REAL protection write point #1: validation tx1's
    claim -> VALIDATING entry refuses with the typed 409 while an old
    version's deletion is in flight, and the whole tx1 rolls back —
    the claim stays CLAIMED, the submission stays UPLOADED, no run row
    exists."""
    from app.core.clock import FrozenClock
    from app.modules.submissions.cleanup_claim import CleanupClaimConflictError
    from app.modules.submissions.models import Submission, SubmissionValidation
    from app.modules.submissions.validation_runner import (
        SandboxLimits,
        ValidatorSandbox,
    )
    from app.modules.submissions.validation_service import ValidationService
    from app.modules.tasks.models import AssignmentClaim

    maker = _factory()
    run = uuid.uuid4().hex[:8]
    world = asyncio.run(
        _seed_world(
            maker,
            run,
            submissions=(
                # v1: overdue, claimed by the cleanup worker, deletion
                # in flight (claimed, unmarked).
                SubmissionSpec(
                    name="old_due",
                    version=1,
                    retention_until=NOW - timedelta(days=30),
                    cleanup_claimed_at=NOW - timedelta(seconds=2),
                ),
                # v2: fresh upload waiting for validation.
                SubmissionSpec(
                    name="fresh",
                    version=2,
                    retention_until=NOW + timedelta(days=180),
                    validation_status="UPLOADED",
                    mark_latest=True,
                ),
            ),
        )
    )
    service = ValidationService(
        clock=FrozenClock(NOW),
        storage=FakeObjectStorage(),
        sandbox=ValidatorSandbox(
            limits=SandboxLimits(
                wall_timeout_seconds=60.0,
                memory_limit_bytes=1024 * 1024 * 1024,
                cpu_seconds=60.0,
            )
        ),
    )

    async def _observe() -> tuple[str, str, int]:
        async with maker() as session:
            claim_status = await session.scalar(
                select(AssignmentClaim.status).where(
                    AssignmentClaim.id == world.claim_id
                )
            )
            submission_status = await session.scalar(
                select(Submission.validation_status).where(
                    Submission.id == world.submission_ids["fresh"]
                )
            )
            runs = len(
                (
                    await session.execute(
                        select(SubmissionValidation.id).where(
                            SubmissionValidation.submission_id
                            == world.submission_ids["fresh"]
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert claim_status is not None and submission_status is not None
            return claim_status, submission_status, runs

    try:

        async def _call() -> None:
            async with maker() as session:
                await service.validate_submission(
                    session, world.submission_ids["fresh"]
                )

        with pytest.raises(CleanupClaimConflictError):
            asyncio.run(_call())
        claim_status, submission_status, runs = asyncio.run(_observe())
        assert claim_status == "CLAIMED"
        assert submission_status == "UPLOADED"
        assert runs == 0
    finally:
        asyncio.run(_cleanup_world(maker, world))


@pytest.mark.integration
def test_reward_lock_review_entry_rejects_then_proceeds_after_completion() -> None:
    """The REAL protection write point #2: the reward-lock UNDER_REVIEW
    entry refuses with the typed 409 while an old version's deletion is
    in flight (claim stays VALIDATING, no lock history), and the same
    call succeeds once the deletion COMPLETED (deleted_at set): the
    protection re-enters after the seconds-level window closes."""
    from app.core.clock import FrozenClock
    from app.modules.identity.events import InMemoryEventCollector
    from app.modules.submissions.cleanup_claim import CleanupClaimConflictError
    from app.modules.submissions.models import RewardLockHistory, Submission
    from app.modules.submissions.reward_lock_service import RewardLockService
    from app.modules.tasks.models import AssignmentClaim

    maker = _factory()
    run = uuid.uuid4().hex[:8]
    world = asyncio.run(
        _seed_world(
            maker,
            run,
            claim_status="VALIDATING",
            submissions=(
                SubmissionSpec(
                    name="old_due",
                    version=1,
                    retention_until=NOW - timedelta(days=30),
                    cleanup_claimed_at=NOW - timedelta(seconds=2),
                ),
                SubmissionSpec(
                    name="passed",
                    version=2,
                    retention_until=NOW + timedelta(days=180),
                    validation_status="VALIDATED",
                    mark_latest=True,
                ),
            ),
        )
    )
    service = RewardLockService(clock=FrozenClock(NOW), events=InMemoryEventCollector())

    async def _claim_status() -> str:
        async with maker() as session:
            status = await session.scalar(
                select(AssignmentClaim.status).where(
                    AssignmentClaim.id == world.claim_id
                )
            )
            assert status is not None
            return status

    async def _history_count() -> int:
        async with maker() as session:
            return len(
                (
                    await session.execute(
                        select(RewardLockHistory.id).where(
                            RewardLockHistory.claim_id == world.claim_id
                        )
                    )
                )
                .scalars()
                .all()
            )

    async def _call() -> None:
        async with maker() as session:
            await service.on_validation_passed(session, world.submission_ids["passed"])

    try:
        with pytest.raises(CleanupClaimConflictError):
            asyncio.run(_call())
        assert asyncio.run(_claim_status()) == "VALIDATING"
        assert asyncio.run(_history_count()) == 0

        # The deletion completes: claimed AND marked — no conflict.
        async def _complete_deletion() -> None:
            async with maker() as session:
                await session.execute(
                    update(Submission)
                    .where(Submission.id == world.submission_ids["old_due"])
                    .values(deleted_at=NOW)
                )
                await session.commit()

        asyncio.run(_complete_deletion())
        asyncio.run(_call())
        assert asyncio.run(_claim_status()) == "UNDER_REVIEW"
    finally:
        asyncio.run(_cleanup_world(maker, world))


# --- the orphan-intent cleanup over real PostgreSQL ---------------------------------


@pytest.mark.integration
def test_orphan_intent_cleanup_matrix() -> None:
    """Expired never-finalized intents — open-expired AND burned — are
    claimed and their objects deleted; a FINALIZED intent is never
    collected; a never-uploaded object is idempotent success; the
    second run over the same state issues zero provider calls."""
    from app.workers.jobs.cleanup_files import SubmissionCleanupRepository

    maker = _factory()
    run = uuid.uuid4().hex[:8]
    world = asyncio.run(
        _seed_world(
            maker,
            run,
            submissions=(
                SubmissionSpec(
                    name="finalized_target",
                    version=1,
                    retention_until=NOW + timedelta(days=180),
                    mark_latest=True,
                ),
            ),
            intents=(
                IntentSpec(name="open_expired", expires_at=NOW - timedelta(hours=1)),
                IntentSpec(
                    name="burned",
                    expires_at=NOW - timedelta(hours=1),
                    consumed_at=NOW - timedelta(minutes=50),
                ),
                IntentSpec(
                    name="finalized",
                    expires_at=NOW - timedelta(hours=1),
                    consumed_at=NOW - timedelta(minutes=50),
                    finalized_version=1,
                ),
                IntentSpec(name="still_live", expires_at=NOW + timedelta(minutes=10)),
            ),
        )
    )
    repository = SubmissionCleanupRepository(maker)
    storage = FakeObjectStorage()
    # The burned intent's object exists; the open-expired one never
    # landed (the common never-uploaded shape). The key was seeded in
    # the database, so the object is registered on the fake directly
    # (put_object requires an issued upload URL this key never had).
    burned_key = asyncio.run(_intent_key(maker, world, "burned"))
    _hold(storage, burned_key)

    try:
        summary = asyncio.run(cleanup_orphaned_intents(NOW, repository, storage))

        assert summary == OrphanIntentSummary(scanned=2, deleted=1, missing=1)
        assert storage.deleted_keys == [burned_key]

        async def _states() -> dict[str, Any]:
            from app.modules.submissions.models import UploadIntent as _Intent

            async with maker() as session:
                rows = (
                    await session.execute(
                        select(
                            _Intent.id,
                            _Intent.cleanup_deleted_at,
                            _Intent.finalized_submission_id,
                        ).where(_Intent.claim_id == world.claim_id)
                    )
                ).all()
            by_id = {row[0]: (row[1], row[2]) for row in rows}
            name_by_id = {v: k for k, v in world.intent_ids.items()}
            return {name_by_id[i]: by_id[i] for i in by_id}

        states = asyncio.run(_states())
        assert states["open_expired"][0] == NOW  # claimed + done
        assert states["burned"][0] == NOW
        assert states["finalized"][0] is None  # never touched
        assert states["still_live"][0] is None

        second = asyncio.run(cleanup_orphaned_intents(NOW, repository, storage))
        assert second == OrphanIntentSummary()
        assert storage.deleted_keys == [burned_key]
    finally:
        asyncio.run(_cleanup_world(maker, world))


@pytest.mark.integration
def test_orphan_intent_provider_failure_releases_claim_for_next_scan() -> None:
    """A provider failure during the intent delete RELEASES the claim
    (cleanup_deleted_at cleared): the object survives, the failure is
    counted once, and the next scan re-claims and completes."""
    from app.integrations.errors import TemporaryProviderError
    from app.workers.jobs.cleanup_files import SubmissionCleanupRepository

    maker = _factory()
    run = uuid.uuid4().hex[:8]
    world = asyncio.run(
        _seed_world(
            maker,
            run,
            intents=(IntentSpec(name="stuck", expires_at=NOW - timedelta(hours=1)),),
        )
    )
    repository = SubmissionCleanupRepository(maker)
    storage = FakeObjectStorage()
    key = asyncio.run(_intent_key(maker, world, "stuck"))
    _hold(storage, key)
    storage.fail_with(TemporaryProviderError("s3 throttled"))

    try:
        failed = asyncio.run(cleanup_orphaned_intents(NOW, repository, storage))
        assert failed == OrphanIntentSummary(
            scanned=1,
            failed={OrphanIntentOutcome.FAILED_STORAGE_TEMPORARY.value: 1},
        )

        async def _claim_state() -> Any:
            from app.modules.submissions.models import UploadIntent

            async with maker() as session:
                return await session.scalar(
                    select(UploadIntent.cleanup_deleted_at).where(
                        UploadIntent.id == world.intent_ids["stuck"]
                    )
                )

        assert asyncio.run(_claim_state()) is None

        healed = asyncio.run(cleanup_orphaned_intents(NOW, repository, storage))
        assert healed == OrphanIntentSummary(scanned=1, deleted=1)
        assert storage.deleted_keys == [key]
    finally:
        asyncio.run(_cleanup_world(maker, world))


def _hold(storage: FakeObjectStorage, object_key: str) -> None:
    """Register one DB-seeded key as a stored object on the fake
    (its upload URL was never issued through this fake instance)."""
    from app.integrations.object_storage import ObjectHead

    storage.objects[object_key] = ObjectHead(
        object_key=object_key, size=128, content_type="text/csv"
    )


async def _intent_key(
    maker: async_sessionmaker[AsyncSession], world: World, name: str
) -> str:
    from app.modules.submissions.models import UploadIntent

    async with maker() as session:
        key = await session.scalar(
            select(UploadIntent.object_key).where(
                UploadIntent.id == world.intent_ids[name]
            )
        )
        assert key is not None
        return key


# --- the two-phase job end to end (eager, real wiring; G1/G18) ------------------------


@pytest.fixture
def celery_app(monkeypatch: pytest.MonkeyPatch) -> Any:
    """TEST-ONLY eager app (the test_expire_claims pattern): `.delay()`
    runs inline and no broker socket is ever opened."""
    import os

    from app.core.config import Settings
    from app.workers.celery_app import create_celery_app

    for name, value in {
        "DATABASE_URL": _database_url(),
        "REDIS_URL": os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        "S3_ENDPOINT_URL": "http://localhost:9000",
        "S3_BUCKET": "campusquest-test",
        "S3_ACCESS_KEY": "campusquest",
        "S3_SECRET_KEY": "campusquest-dev",
        "BUSINESS_TIMEZONE": "Asia/Shanghai",
    }.items():
        monkeypatch.setenv(name, value)
    app = create_celery_app(Settings())
    app.conf.task_always_eager = True
    return app


@pytest.mark.integration
def test_cleanup_scan_job_runs_both_phases_end_to_end(celery_app: Any) -> None:
    """The REAL task, inline: SystemClock sample, shared per-job session
    source, the production S3 adapter, BOTH scan phases in one payload
    (files, then orphan intents). The seeded objects never landed in
    MinIO, so both phases prove their §27 idempotent-missing branches
    through the REAL adapter: the due submission reconciles (claimed,
    delete 404s, marked deleted) and the expired intent answers
    missing-object (claimed, done). A second scan finds nothing."""
    import json
    from datetime import UTC as _UTC

    from app.core.config import get_settings
    from app.modules.submissions.models import Submission, UploadIntent
    from app.workers.jobs.cleanup_files import cleanup_files_scan

    get_settings.cache_clear()
    maker = _factory()
    run = uuid.uuid4().hex[:8]
    real_now = datetime.now(_UTC)
    world = asyncio.run(
        _seed_world(
            maker,
            run,
            submissions=(
                SubmissionSpec(
                    name="due",
                    version=1,
                    retention_until=real_now - timedelta(days=30),
                    mark_latest=True,
                ),
            ),
            intents=(
                IntentSpec(name="orphan", expires_at=real_now - timedelta(hours=1)),
            ),
        )
    )
    try:
        scan = cleanup_files_scan.delay("req-cleanup-1").get(timeout=60)
        assert scan["request_id"] == "req-cleanup-1"
        assert scan["scanned"] == 1
        assert scan["reconciled"] == 1
        assert scan["deleted"] == 0
        assert scan["intents_scanned"] == 1
        assert scan["intents_missing"] == 1
        assert scan["intents_deleted"] == 0
        # JSON wire contract: the summary round-trips through json.
        assert json.loads(json.dumps(scan)) == scan

        async def _observe() -> tuple[Any, Any, Any]:
            async with maker() as session:
                submission = await session.get(Submission, world.submission_ids["due"])
                intent = await session.get(UploadIntent, world.intent_ids["orphan"])
                assert submission is not None and intent is not None
                return (
                    submission.cleanup_claimed_at,
                    submission.deleted_at,
                    intent.cleanup_deleted_at,
                )

        claimed_at, deleted_at, intent_done = asyncio.run(_observe())
        assert claimed_at is not None  # the claim that authorized it
        assert deleted_at is not None  # the reconcile completion mark
        assert intent_done is not None

        again = cleanup_files_scan.delay("req-cleanup-2").get(timeout=60)
        assert again["scanned"] == 0
        assert again["intents_scanned"] == 0
    finally:
        asyncio.run(_cleanup_world(maker, world))
        get_settings.cache_clear()
