# backend/tests/integration/submissions/test_upload_finalize.py
"""Presigned upload intent + finalize end-to-end against real PostgreSQL
and the FakeObjectStorage port (spec §10 steps 1-7, §11, §13, §31.11,
§32; backend-engineering §13: tests use fakes).

Scenarios:

- **Full flow:** create intent (server-generated key
  ``submissions/{claim_id}/{uuid}``, sanitized display filename, short
  TTL), client completes the presigned PUT (``put_object``), finalize
  verifies ``head_object`` and writes Submission version 1 with
  ``submitted_at`` from the FrozenClock and the §13 retention snapshot
  computed AT FINALIZE TIME — all four policies parameterized.
- **Duplicate finalize (spec §32 upload-complete idempotency):** the
  second call returns the SAME Submission (id and version), and no
  version 2 ever exists — the intent is single-use.
- **Replay after the claim moved on (T2 carry, plan 04 task 7):** a
  delayed finalize replay of an already-finalized intent returns the
  SAME Submission even when the claim has since transitioned to
  VALIDATING (the validation worker's claim state) — the replay check
  runs BEFORE the claim-submittability gate, so the no-duplicate
  invariant does not degrade into a confusing CLAIM_NOT_SUBMITTABLE.
- **Size mismatch:** ``head_object`` reports a size different from the
  declaration -> typed FILE_TOO_LARGE error, no Submission row, and the
  intent is burned (replays answer intent-not-found; the remedy is a new
  intent).
- **Content-type mismatch:** the stored object's content type disagrees
  with the declared type's pinned MIME -> FILE_TYPE_NOT_ALLOWED, burned
  intent, no Submission.
- **Missing object:** finalize before the upload completes answers a
  typed NOT_FOUND and leaves the intent consumable — the retry after the
  object lands succeeds.
- **Intent expiry:** finalizing after ``expires_at`` answers
  intent-not-found.
- **Window recheck at finalize:** time may have passed between intent
  and finalize; the window is recomputed and a finalize past grace is
  SUBMISSION_WINDOW_CLOSED with no Submission written.
- **Concurrent finalize:** two independent sessions released on one
  barrier finalize the SAME intent -> both return the SAME Submission
  and the database holds exactly one row (the conditional
  ``consumed_at IS NULL`` consume plus the claim-row lock serialize the
  pair into create + idempotent replay).

Harness notes: seeding, service calls, and assertions use independent
committed sessions from the engine factory (the savepoint-wrapped
``db_session`` fixture is invisible across connections); every test
removes its rows with explicit committed DELETEs in ``finally``
(upload_intents -> submissions -> claims -> assignments -> tasks ->
users, the FK order). Usernames embed a per-run token, so rows leaked
by an aborted run can never collide with a later seeding pass.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.clock import FrozenClock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.submissions.enums import (
    RetentionPolicy,
    ReviewStatus,
    ValidationStatus,
)
from app.modules.submissions.models import Submission, UploadIntent
from app.modules.submissions.upload_service import UploadService
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
from tests.fakes.integrations import FakeObjectStorage

# Direct-insert password stub (argon2 hash of an unguessable test secret);
# the registration service is deliberately not exercised here.
_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)

_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
_GRACE = _NOW + timedelta(hours=6)
_DECLARED_SIZE = 4096
_INTENT_TTL = timedelta(minutes=15)


# --- seeding helpers ---------------------------------------------------------------


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
        "allowed_file_types": ["CSV", "XLSX"],
        "max_file_size_bytes": 10 * 1024 * 1024,
        "notification_channels": ["SMS"],
        "retention_policy": RetentionPolicy.DAYS_180,
    }
    fields.update(overrides)
    return Task(**fields)


def _assignment(task: Task, *, keyword: str = "考研英语") -> Assignment:
    return Assignment(
        task_id=task.id,
        platform="xiaohongshu",
        keyword=keyword,
        availability_status=AssignmentAvailability.OCCUPIED,
    )


def _claim(
    assignment: Assignment,
    student: User,
    *,
    status: ClaimStatus = ClaimStatus.CLAIMED,
    grace_deadline_at: datetime = _GRACE,
    revision_deadline_at: datetime | None = None,
) -> AssignmentClaim:
    return AssignmentClaim(
        assignment_id=assignment.id,
        task_id=assignment.task_id,
        user_id=student.id,
        status=status,
        claimed_at=_NOW - timedelta(days=1),
        deadline_at=grace_deadline_at - timedelta(minutes=1440),
        grace_deadline_at=grace_deadline_at,
        reward_policy_snapshot={"version": 1},
        base_reward_points_snapshot=100,
        submission_schema_version=1,
        reward_lock_status=RewardLockStatus.NONE,
        revision_deadline_at=revision_deadline_at,
    )


@dataclass(slots=True)
class Seed:
    """The committed graph one test operates on."""

    teacher: User
    student: User
    task: Task
    assignment: Assignment
    claim: AssignmentClaim


async def _seed(factory: async_sessionmaker[AsyncSession], run: str) -> Seed:
    """Commit the full parent chain; ids are server-generated on flush."""
    async with factory() as session:
        teacher = _user(username=f"t{run}", role=Role.TEACHER)
        student = _user(username=f"2025{run}001")
        session.add_all((teacher, student))
        await session.flush()
        task = _task(teacher)
        session.add(task)
        await session.flush()
        assignment = _assignment(task)
        session.add(assignment)
        await session.flush()
        claim = _claim(assignment, student)
        session.add(claim)
        await session.commit()
        return Seed(
            teacher=teacher,
            student=student,
            task=task,
            assignment=assignment,
            claim=claim,
        )


async def _cleanup(
    factory: async_sessionmaker[AsyncSession],
    *,
    task_ids: list[UUID],
    user_ids: list[UUID],
) -> None:
    """Explicit committed cleanup in FK order (upload_intents ->
    submissions -> claims -> assignments -> tasks -> users); nothing
    rolls these rows back for us."""
    async with factory() as session:
        if task_ids:
            claim_ids = select(AssignmentClaim.id).where(
                AssignmentClaim.task_id.in_(task_ids)
            )
            await session.execute(
                delete(UploadIntent).where(UploadIntent.claim_id.in_(claim_ids))
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


def _factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


def _service(
    clock: FrozenClock, storage: FakeObjectStorage | None = None
) -> UploadService:
    storage = storage if storage is not None else FakeObjectStorage(clock=clock)
    return UploadService(clock=clock, storage=storage)


def _actor(student: User) -> Actor:
    return Actor(user_id=student.id, role=Role.STUDENT)


# --- full flow ----------------------------------------------------------------------


@pytest.mark.integration
async def test_full_flow_creates_version_one_submission(
    db_engine: AsyncEngine,
) -> None:
    """Spec §10 steps 1-7: intent -> presigned URL -> client PUT ->
    finalize verifies the object and writes Submission v1 with the
    clock's instant, the declared metadata, and the claim projection."""
    factory = _factory(db_engine)
    clock = FrozenClock(_NOW)
    storage = FakeObjectStorage(clock=clock)
    service = _service(clock, storage)
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = await _seed(factory, run)
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))

        async with factory() as session:
            receipt = await service.create_upload_intent(
                session,
                _actor(seed.student),
                seed.claim.id,
                "../../考研英语笔记.csv",
                "CSV",
                _DECLARED_SIZE,
            )

            # Intent row: single-use grant, sanitized display filename,
            # server-generated key, short TTL (spec §10).
            intent = await session.get(UploadIntent, receipt.intent_id)
            assert intent is not None
            assert intent.claim_id == seed.claim.id
            parts = intent.object_key.split("/")
            assert parts[0] == "submissions"
            assert parts[1] == str(seed.claim.id)
            UUID(parts[2])  # the key tail is a server-generated uuid
            assert intent.declared_type == "CSV"
            assert intent.declared_size == _DECLARED_SIZE
            assert intent.filename == "考研英语笔记.csv"
            assert intent.consumed_at is None
            assert intent.finalized_submission_id is None
            assert intent.expires_at == _NOW + _INTENT_TTL
            assert intent.created_at is not None

            # The receipt hands the client the short-lived presigned URL.
            assert receipt.object_key == intent.object_key
            assert receipt.upload_url.startswith("https://")
            assert receipt.upload_url.endswith(intent.object_key)
            assert receipt.url_expires_at == _NOW + timedelta(minutes=10)

            storage.put_object(object_key=intent.object_key, size=_DECLARED_SIZE)
            submission = await service.finalize_upload(
                session, _actor(seed.student), receipt.intent_id
            )

            assert submission.claim_id == seed.claim.id
            assert submission.version == 1
            assert submission.object_key == intent.object_key
            assert submission.original_filename == "考研英语笔记.csv"
            assert submission.declared_type == "CSV"
            assert submission.detected_type is None
            assert submission.file_size == _DECLARED_SIZE
            assert submission.submitted_at == _NOW
            assert submission.validation_status == ValidationStatus.UPLOADED
            assert submission.review_status == ReviewStatus.PENDING_REVIEW

            # The intent is consumed exactly once and points at its
            # submission; the claim projection follows (spec §11).
            await session.refresh(intent)
            assert intent.consumed_at == _NOW
            assert intent.finalized_submission_id == submission.id
            claim_row = await session.get(AssignmentClaim, seed.claim.id)
            assert claim_row is not None
            assert claim_row.latest_submission_id == submission.id
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("policy", "days"),
    [
        (RetentionPolicy.DAYS_30, 30),
        (RetentionPolicy.DAYS_90, 90),
        (RetentionPolicy.DAYS_180, 180),
        (RetentionPolicy.PERMANENT, None),
    ],
)
async def test_retention_snapshot_at_finalize_for_all_policies(
    db_engine: AsyncEngine, policy: RetentionPolicy, days: int | None
) -> None:
    """Spec §13: the snapshot is computed from the Task's CURRENT policy
    at finalize time — dated policies store submitted_at + N days, and
    permanent is the explicit flag with a NULL expiry."""
    factory = _factory(db_engine)
    clock = FrozenClock(_NOW)
    storage = FakeObjectStorage(clock=clock)
    service = _service(clock, storage)
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        async with factory() as session:
            teacher = _user(username=f"t{run}", role=Role.TEACHER)
            student = _user(username=f"2025{run}001")
            session.add_all((teacher, student))
            await session.flush()
            task = _task(teacher, retention_policy=policy)
            session.add(task)
            await session.flush()
            assignment = _assignment(task)
            session.add(assignment)
            await session.flush()
            claim = _claim(assignment, student)
            session.add(claim)
            await session.commit()
            task_ids.append(task.id)
            user_ids.extend((teacher.id, student.id))

        async with factory() as session:
            receipt = await service.create_upload_intent(
                session, _actor(student), claim.id, "数据.csv", "CSV", _DECLARED_SIZE
            )
            storage.put_object(object_key=receipt.object_key, size=_DECLARED_SIZE)
            submission = await service.finalize_upload(
                session, _actor(student), receipt.intent_id
            )

            if days is None:
                assert submission.retention_until is None
                assert submission.retention_permanent is True
            else:
                assert submission.retention_until == _NOW + timedelta(days=days)
                assert submission.retention_permanent is False
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


@pytest.mark.integration
async def test_task_policy_edit_after_finalize_never_rewrites_snapshot(
    db_engine: AsyncEngine,
) -> None:
    """Spec §13's motivating case: editing the Task's retention policy
    after the submission exists leaves the snapshot untouched."""
    factory = _factory(db_engine)
    clock = FrozenClock(_NOW)
    storage = FakeObjectStorage(clock=clock)
    service = _service(clock, storage)
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = await _seed(factory, run)
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))

        async with factory() as session:
            receipt = await service.create_upload_intent(
                session,
                _actor(seed.student),
                seed.claim.id,
                "数据.csv",
                "CSV",
                _DECLARED_SIZE,
            )
            storage.put_object(object_key=receipt.object_key, size=_DECLARED_SIZE)
            submission = await service.finalize_upload(
                session, _actor(seed.student), receipt.intent_id
            )
            assert submission.retention_permanent is False
            assert submission.retention_until == _NOW + timedelta(days=180)

        async with factory() as session:
            task = await session.get(Task, seed.task.id)
            assert task is not None
            task.retention_policy = RetentionPolicy.PERMANENT
            await session.commit()

        async with factory() as session:
            reloaded = await session.get(Submission, submission.id)
            assert reloaded is not None
            assert reloaded.retention_permanent is False
            assert reloaded.retention_until == _NOW + timedelta(days=180)
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- duplicate finalize (spec §32) --------------------------------------------------


@pytest.mark.integration
async def test_duplicate_finalize_returns_same_submission(
    db_engine: AsyncEngine,
) -> None:
    """Upload-complete callback idempotency (spec §32): the replay
    returns the SAME Submission (id and version) and never creates
    version 2."""
    factory = _factory(db_engine)
    clock = FrozenClock(_NOW)
    storage = FakeObjectStorage(clock=clock)
    service = _service(clock, storage)
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = await _seed(factory, run)
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))

        async with factory() as session:
            receipt = await service.create_upload_intent(
                session,
                _actor(seed.student),
                seed.claim.id,
                "数据.csv",
                "CSV",
                _DECLARED_SIZE,
            )
            storage.put_object(object_key=receipt.object_key, size=_DECLARED_SIZE)
            first = await service.finalize_upload(
                session, _actor(seed.student), receipt.intent_id
            )

        async with factory() as session:
            second = await service.finalize_upload(
                session, _actor(seed.student), receipt.intent_id
            )
            assert second.id == first.id
            assert second.version == 1
            assert second.submitted_at == _NOW

            versions = (
                await session.scalars(
                    select(Submission.version).where(
                        Submission.claim_id == seed.claim.id
                    )
                )
            ).all()
            assert versions == [1]
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- verification failures ----------------------------------------------------------


@pytest.mark.integration
async def test_size_mismatch_fails_finalize_burns_intent(
    db_engine: AsyncEngine,
) -> None:
    """head_object size != declared size (spec §10 step 6): typed
    FILE_TOO_LARGE, no Submission, and the corrupt intent is consumed
    without a submission — the remedy is a fresh intent."""
    factory = _factory(db_engine)
    clock = FrozenClock(_NOW)
    storage = FakeObjectStorage(clock=clock)
    service = _service(clock, storage)
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = await _seed(factory, run)
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))

        async with factory() as session:
            receipt = await service.create_upload_intent(
                session,
                _actor(seed.student),
                seed.claim.id,
                "数据.csv",
                "CSV",
                _DECLARED_SIZE,
            )
            storage.put_object(
                object_key=receipt.object_key, size=_DECLARED_SIZE + 1024
            )
            with pytest.raises(Exception) as excinfo:
                await service.finalize_upload(
                    session, _actor(seed.student), receipt.intent_id
                )
            error = excinfo.value
            assert error.code == "FILE_TOO_LARGE"
            assert error.status_code == 400
            assert error.details["declared_size"] == _DECLARED_SIZE
            assert error.details["actual_size"] == _DECLARED_SIZE + 1024

            assert (
                await session.scalars(
                    select(Submission).where(Submission.claim_id == seed.claim.id)
                )
            ).all() == []

            intent = await session.get(UploadIntent, receipt.intent_id)
            assert intent is not None
            assert intent.consumed_at == _NOW
            assert intent.finalized_submission_id is None

        async with factory() as session:
            with pytest.raises(Exception) as excinfo:
                await service.finalize_upload(
                    session, _actor(seed.student), receipt.intent_id
                )
            assert excinfo.value.code == "NOT_FOUND"
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


@pytest.mark.integration
async def test_content_type_mismatch_fails_finalize(
    db_engine: AsyncEngine,
) -> None:
    """The stored object's content type disagrees with the declared
    type's pinned MIME (the presigned URL pins it; this is the
    defense-in-depth recheck): FILE_TYPE_NOT_ALLOWED, burned intent, no
    Submission."""
    from app.integrations.object_storage import ObjectHead

    factory = _factory(db_engine)
    clock = FrozenClock(_NOW)
    storage = FakeObjectStorage(clock=clock)
    service = _service(clock, storage)
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = await _seed(factory, run)
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))

        async with factory() as session:
            receipt = await service.create_upload_intent(
                session,
                _actor(seed.student),
                seed.claim.id,
                "数据.csv",
                "CSV",
                _DECLARED_SIZE,
            )
            storage.put_object(object_key=receipt.object_key, size=_DECLARED_SIZE)
            storage.objects[receipt.object_key] = ObjectHead(
                object_key=receipt.object_key,
                size=_DECLARED_SIZE,
                content_type="application/x-protobuf",
            )
            with pytest.raises(Exception) as excinfo:
                await service.finalize_upload(
                    session, _actor(seed.student), receipt.intent_id
                )
            error = excinfo.value
            assert error.code == "FILE_TYPE_NOT_ALLOWED"
            assert (
                await session.scalars(
                    select(Submission).where(Submission.claim_id == seed.claim.id)
                )
            ).all() == []
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


@pytest.mark.integration
async def test_missing_object_is_retryable(
    db_engine: AsyncEngine,
) -> None:
    """Finalize before the client's upload lands: typed NOT_FOUND, no
    Submission, and the intent STAYS consumable — the retry after the
    object exists succeeds (a missing object is a race, not a corrupt
    upload)."""
    factory = _factory(db_engine)
    clock = FrozenClock(_NOW)
    storage = FakeObjectStorage(clock=clock)
    service = _service(clock, storage)
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = await _seed(factory, run)
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))

        async with factory() as session:
            receipt = await service.create_upload_intent(
                session,
                _actor(seed.student),
                seed.claim.id,
                "数据.csv",
                "CSV",
                _DECLARED_SIZE,
            )
            with pytest.raises(Exception) as excinfo:
                await service.finalize_upload(
                    session, _actor(seed.student), receipt.intent_id
                )
            error = excinfo.value
            assert error.code == "NOT_FOUND"
            assert error.status_code == 404

            intent = await session.get(UploadIntent, receipt.intent_id)
            assert intent is not None
            assert intent.consumed_at is None

            storage.put_object(object_key=receipt.object_key, size=_DECLARED_SIZE)
            submission = await service.finalize_upload(
                session, _actor(seed.student), receipt.intent_id
            )
            assert submission.version == 1
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


@pytest.mark.integration
async def test_expired_intent_refused(
    db_engine: AsyncEngine,
) -> None:
    """Finalizing after expires_at answers intent-not-found; nothing is
    written."""
    factory = _factory(db_engine)
    storage = FakeObjectStorage(clock=FrozenClock(_NOW))
    service = _service(FrozenClock(_NOW), storage)
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = await _seed(factory, run)
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))

        async with factory() as session:
            receipt = await service.create_upload_intent(
                session,
                _actor(seed.student),
                seed.claim.id,
                "数据.csv",
                "CSV",
                _DECLARED_SIZE,
            )
        storage.put_object(object_key=receipt.object_key, size=_DECLARED_SIZE)

        later = UploadService(
            clock=FrozenClock(_NOW + _INTENT_TTL + timedelta(seconds=1)),
            storage=storage,
        )
        async with factory() as session:
            with pytest.raises(Exception) as excinfo:
                await later.finalize_upload(
                    session, _actor(seed.student), receipt.intent_id
                )
            assert excinfo.value.code == "NOT_FOUND"
            assert (
                await session.scalars(
                    select(Submission).where(Submission.claim_id == seed.claim.id)
                )
            ).all() == []
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


@pytest.mark.integration
async def test_finalize_recomputes_window(
    db_engine: AsyncEngine,
) -> None:
    """Time may have passed between intent and finalize: the window is
    recomputed at finalize and a claim past grace refuses with
    SUBMISSION_WINDOW_CLOSED, writing nothing. The claim's grace sits 5
    minutes out so the late finalize (6 minutes) is past grace but still
    inside the 15-minute intent TTL — isolating the window recheck from
    the expiry check."""
    factory = _factory(db_engine)
    storage = FakeObjectStorage(clock=FrozenClock(_NOW))
    run = uuid4().hex[:8]
    tight_grace = _NOW + timedelta(minutes=5)

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        async with factory() as session:
            teacher = _user(username=f"t{run}", role=Role.TEACHER)
            student = _user(username=f"2025{run}001")
            session.add_all((teacher, student))
            await session.flush()
            task = _task(teacher)
            session.add(task)
            await session.flush()
            assignment = _assignment(task)
            session.add(assignment)
            await session.flush()
            claim = _claim(assignment, student, grace_deadline_at=tight_grace)
            session.add(claim)
            await session.commit()
            task_ids.append(task.id)
            user_ids.extend((teacher.id, student.id))

        async with factory() as session:
            service = _service(FrozenClock(_NOW), storage)
            receipt = await service.create_upload_intent(
                session, _actor(student), claim.id, "数据.csv", "CSV", _DECLARED_SIZE
            )
        storage.put_object(object_key=receipt.object_key, size=_DECLARED_SIZE)

        late_service = _service(
            FrozenClock(tight_grace + timedelta(minutes=1)), storage
        )
        async with factory() as session:
            with pytest.raises(Exception) as excinfo:
                await late_service.finalize_upload(
                    session, _actor(student), receipt.intent_id
                )
            error = excinfo.value
            assert error.code == "SUBMISSION_WINDOW_CLOSED"
            assert error.status_code == 409
            assert (
                await session.scalars(
                    select(Submission).where(Submission.claim_id == claim.id)
                )
            ).all() == []
            intent = await session.get(UploadIntent, receipt.intent_id)
            assert intent is not None
            assert intent.consumed_at is None
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


# --- concurrency --------------------------------------------------------------------


@dataclass(slots=True)
class FinalizeOutcome:
    """One finalize attempt's terminal state: a submission, a business
    error, or — never, if the service is correct — an unexpected
    exception."""

    submission: Submission | None = None
    error: Exception | None = None
    unexpected: BaseException | None = None


async def _finalize_one(
    service: UploadService,
    factory: async_sessionmaker[AsyncSession],
    actor: Actor,
    intent_id: UUID,
    start: asyncio.Event,
) -> FinalizeOutcome:
    async with factory() as session:
        # Warm the pooled connection BEFORE the barrier (the claim
        # concurrency harness note): connection setup must not dwarf the
        # locked critical section the test exists to exercise.
        await session.execute(text("SELECT 1"))
        await start.wait()
        try:
            submission = await service.finalize_upload(session, actor, intent_id)
        except BusinessError as exc:
            return FinalizeOutcome(error=exc)
        except Exception as exc:  # the "no 500" failure mode
            return FinalizeOutcome(unexpected=exc)
        return FinalizeOutcome(submission=submission)


@pytest.mark.integration
async def test_concurrent_finalize_of_same_intent_yields_one_submission(
    db_engine: AsyncEngine,
) -> None:
    """Two sessions on one barrier finalize the same intent: both return
    the SAME Submission, the database holds exactly one row, and the
    version stays 1 (spec §32 + §31.11)."""
    factory = _factory(db_engine)
    clock = FrozenClock(_NOW)
    storage = FakeObjectStorage(clock=clock)
    service = _service(clock, storage)
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = await _seed(factory, run)
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))

        async with factory() as session:
            receipt = await service.create_upload_intent(
                session,
                _actor(seed.student),
                seed.claim.id,
                "数据.csv",
                "CSV",
                _DECLARED_SIZE,
            )
        storage.put_object(object_key=receipt.object_key, size=_DECLARED_SIZE)

        start = asyncio.Event()
        tasks = [
            asyncio.create_task(
                _finalize_one(
                    service, factory, _actor(seed.student), receipt.intent_id, start
                )
            )
            for _ in range(2)
        ]
        await asyncio.sleep(0.05)  # park every transaction on one barrier
        start.set()
        outcomes = list(await asyncio.gather(*tasks))

        blown_up = [o for o in outcomes if o.unexpected is not None]
        assert not blown_up, [repr(o.unexpected) for o in blown_up]
        errored = [o for o in outcomes if o.error is not None]
        assert not errored, [repr(o.error) for o in errored]
        submissions = [o.submission for o in outcomes if o.submission is not None]
        assert len(submissions) == 2
        assert {s.id for s in submissions} == {submissions[0].id}
        assert all(s.version == 1 for s in submissions)

        async with factory() as session:
            rows = (
                await session.scalars(
                    select(Submission).where(Submission.claim_id == seed.claim.id)
                )
            ).all()
            assert len(rows) == 1
            assert rows[0].version == 1
            intent = await session.get(UploadIntent, receipt.intent_id)
            assert intent is not None
            assert intent.consumed_at == _NOW
            assert intent.finalized_submission_id == rows[0].id
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


@pytest.mark.integration
async def test_finalize_replay_after_claim_moved_to_validating_returns_same_submission(
    db_engine: AsyncEngine,
) -> None:
    """T2 carry regression (fixed in plan 04 task 7): the intent-replay
    check precedes the claim-submittability gate, so a delayed replay
    against a claim the validation worker already moved to VALIDATING
    returns the SAME Submission — never CLAIM_NOT_SUBMITTABLE, never a
    version 2."""
    factory = _factory(db_engine)
    clock = FrozenClock(_NOW)
    storage = FakeObjectStorage(clock=clock)
    service = _service(clock, storage)
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = await _seed(factory, run)
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))

        async with factory() as session:
            receipt = await service.create_upload_intent(
                session,
                _actor(seed.student),
                seed.claim.id,
                "数据.csv",
                "CSV",
                _DECLARED_SIZE,
            )
        storage.put_object(object_key=receipt.object_key, size=_DECLARED_SIZE)
        async with factory() as session:
            first = await service.finalize_upload(
                session, _actor(seed.student), receipt.intent_id
            )

        # The validation worker's claim transition (task 8 owns the real
        # write; here it is simulated directly): CLAIMED -> VALIDATING.
        async with factory() as session:
            await session.execute(
                update(AssignmentClaim)
                .where(AssignmentClaim.id == seed.claim.id)
                .values(status=ClaimStatus.VALIDATING.value)
            )
            await session.commit()

        async with factory() as session:
            replay = await service.finalize_upload(
                session, _actor(seed.student), receipt.intent_id
            )

        assert replay.id == first.id
        assert replay.version == 1

        async with factory() as session:
            rows = (
                await session.scalars(
                    select(Submission).where(Submission.claim_id == seed.claim.id)
                )
            ).all()
            assert len(rows) == 1
            assert rows[0].version == 1
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)


@pytest.mark.integration
async def test_open_intent_finalize_on_validating_claim_is_not_submittable(
    db_engine: AsyncEngine,
) -> None:
    """The moved gate's own code-order path (the create-side gate is
    covered by the intent-creation tests): an OPEN — not a replay —
    intent whose claim has since moved to VALIDATING answers the typed
    409 CLAIM_NOT_SUBMITTABLE, writes no Submission, and leaves the
    intent consumable."""
    factory = _factory(db_engine)
    clock = FrozenClock(_NOW)
    storage = FakeObjectStorage(clock=clock)
    service = _service(clock, storage)
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = await _seed(factory, run)
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))

        # Intent issued while the claim was CLAIMED; the upload lands.
        async with factory() as session:
            receipt = await service.create_upload_intent(
                session,
                _actor(seed.student),
                seed.claim.id,
                "数据.csv",
                "CSV",
                _DECLARED_SIZE,
            )
        storage.put_object(object_key=receipt.object_key, size=_DECLARED_SIZE)

        # The claim moves on before the client finalizes (the validation
        # worker's claim transition, simulated directly as in the replay
        # test above).
        async with factory() as session:
            await session.execute(
                update(AssignmentClaim)
                .where(AssignmentClaim.id == seed.claim.id)
                .values(status=ClaimStatus.VALIDATING.value)
            )
            await session.commit()

        async with factory() as session:
            with pytest.raises(BusinessError) as excinfo:
                await service.finalize_upload(
                    session, _actor(seed.student), receipt.intent_id
                )
        assert excinfo.value.code is ErrorCode.CLAIM_NOT_SUBMITTABLE
        assert excinfo.value.status_code == 409

        async with factory() as session:
            rows = (
                await session.scalars(
                    select(Submission).where(Submission.claim_id == seed.claim.id)
                )
            ).all()
            assert rows == []
            intent = await session.get(UploadIntent, receipt.intent_id)
            assert intent is not None
            assert intent.consumed_at is None  # still consumable
            assert intent.finalized_submission_id is None
    finally:
        await _cleanup(factory, task_ids=task_ids, user_ids=user_ids)
