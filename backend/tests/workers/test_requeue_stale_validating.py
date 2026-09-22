# backend/tests/workers/test_requeue_stale_validating.py
"""Stale-VALIDATING + stale-UPLOADED recovery scan tests (PR #2
hardening, final-review sub-F2 + final pass B P1; spec §10 step 8 /
§32).

The discovery predicates run against real PostgreSQL. Family 1 (stale
VALIDATING): a submission stuck in VALIDATING whose NEWEST run row
started before the threshold qualifies; a fresh in-flight run (newest
run row recent) does not; a terminal submission never does even when it
carries an old interrupted run row in its history (the terminal gate is
the projection, not the history). Family 2 (stale UPLOADED): a
submission still UPLOADED whose ``submitted_at`` predates the dispatch
grace qualifies; a fresh finalize (its enqueue merely in flight) does
not. The wiring itself — task registration, autoretry, the beat entry —
is pinned by test_celery_wiring.py.

The task-level recovery tests (the owner-named three) run the REAL scan
task in eager Celery mode over the REAL database, with the scan's
session source and the re-dispatched validation job's storage/session
defaults injected (the test_validate_submission_job_e2e pattern): a
finalize whose post-commit dispatch raised leaves the row UPLOADED and
the scan re-dispatches it to completion; a validation whose every tx1
was refused by a live cleanup deletion claim (CleanupClaimConflictError
— the Celery ladder exhausts long inside the lease) leaves the row
UPLOADED and the scan heals it once the deletion completes; and a fresh
UPLOADED row below the grace is never dispatched.

Harness notes (the test_expire_claims conventions): explicit committed
sessions from a NullPool engine; real commits require real cleanup in
FK order (validations -> submissions -> claims -> assignments -> tasks
-> users); usernames embed a per-run token.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.clock import FrozenClock
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.submissions.models import Submission, SubmissionValidation
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
from app.workers.jobs.requeue_stale_validating import (
    collect_stale_uploaded_ids,
    collect_stale_validating_ids,
)
from tests.fakes.integrations import FakeObjectStorage

pytestmark = pytest.mark.integration

_TEST_DATABASE_MARKER = "campusquest_test"
_DEFAULT_DATABASE_URL = (
    "postgresql+asyncpg://test:test@localhost:15432/campusquest_test"
)

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)

_NOW = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
_GRACE = _NOW + timedelta(hours=6)

# The task schema + content the eager validation job actually parses: a
# valid CSV the real sandboxed validator passes.
_CSV_SCHEMA = {
    "required_columns": [
        {"name": "url", "type": "string", "unique": True},
        {"name": "title", "type": "string"},
    ]
}
_VALID_CSV = b"url,title\nhttps://a.com,t\nhttps://b.com,t2\n"


def _database_url() -> str:
    """The integration database URL, refusing non-test databases."""
    from sqlalchemy.engine import make_url

    url = os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)
    database = make_url(url).database or ""
    if _TEST_DATABASE_MARKER not in database:
        pytest.fail(
            f"Refusing requeue-scan tests against non-test database "
            f"{database!r} (DATABASE_URL={url!r}): the database name must "
            f"contain {_TEST_DATABASE_MARKER!r}."
        )
    return url


def specs_names() -> tuple[str, ...]:
    return ("stale", "fresh", "terminal_history")


def _factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed_world(
    maker: async_sessionmaker[AsyncSession], run: str
) -> dict[str, UUID]:
    """Three submissions over three claims, one task:

    - ``stale``: VALIDATING, its only run row started 2h ago (threshold
      30 min) — the wedged shape, MUST qualify;
    - ``fresh``: VALIDATING, a fresh run row started 5 min ago (an
      in-flight retry ladder) — MUST NOT qualify;
    - ``terminal_history``: VALIDATION_FAILED projection carrying an
      interrupted VALIDATING run row from 2h ago in its history — MUST
      NOT qualify (the projection is the gate, the history is honest
      history).
    """
    async with maker() as session:
        teacher = User(
            username=f"t{run}",
            password_hash=_PASSWORD_HASH,
            nickname=f"老师{run[-4:]}",
            phone_e164=None,
            role=Role.TEACHER,
            status=UserStatus.ACTIVE,
        )
        session.add(teacher)
        await session.flush()
        # One student per scenario: the ACTIVE-claim partial unique
        # index (user_id, task_id) forbids two concurrent claims of one
        # task by the same student.
        students: dict[str, User] = {}
        for index, name in enumerate(specs_names()):
            student = User(
                username=f"2025{run}00{index + 1}",
                password_hash=_PASSWORD_HASH,
                nickname=f"同学{run[-4:]}",
                phone_e164=None,
                role=Role.STUDENT,
                status=UserStatus.ACTIVE,
            )
            session.add(student)
            students[name] = student
        await session.flush()
        task = Task(
            owner_teacher_id=teacher.id,
            title="校园食堂满意度问卷采集",
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
        ids: dict[str, UUID] = {}
        specs = {
            "stale": ("VALIDATING", _NOW - timedelta(hours=2)),
            "fresh": ("VALIDATING", _NOW - timedelta(minutes=5)),
            "terminal_history": ("VALIDATION_FAILED", _NOW - timedelta(hours=2)),
        }
        for name, (validation_status, run_started) in specs.items():
            assignment = Assignment(
                task_id=task.id,
                platform="xiaohongshu",
                keyword=f"食堂{name}",
                availability_status=AssignmentAvailability.OCCUPIED,
            )
            session.add(assignment)
            await session.flush()
            claim = AssignmentClaim(
                assignment_id=assignment.id,
                task_id=task.id,
                user_id=students[name].id,
                status=ClaimStatus.VALIDATING.value,
                claimed_at=_NOW - timedelta(days=3),
                deadline_at=_NOW - timedelta(days=1),
                grace_deadline_at=_NOW - timedelta(hours=20),
                reward_policy_snapshot={"version": 1},
                base_reward_points_snapshot=100,
                submission_schema_version=1,
                reward_lock_status=RewardLockStatus.NONE,
            )
            session.add(claim)
            await session.flush()
            submission = Submission(
                claim_id=claim.id,
                version=1,
                object_key=f"submissions/{claim.id}/{uuid4()}",
                original_filename="数据.csv",
                declared_type="CSV",
                file_size=1024,
                submitted_at=_NOW - timedelta(days=1),
                validation_status=validation_status,
                retention_until=_NOW + timedelta(days=180),
            )
            session.add(submission)
            await session.flush()
            claim.latest_submission_id = submission.id
            session.add(
                SubmissionValidation(
                    submission_id=submission.id,
                    parser_version="pending",
                    status="VALIDATING",
                    started_at=run_started,
                )
            )
            ids[name] = submission.id
        await session.commit()
        ids["task"] = task.id
        ids["teacher"] = teacher.id
        for student in students.values():
            ids[f"student:{student.username}"] = student.id
        return ids


async def _cleanup_world(
    maker: async_sessionmaker[AsyncSession], ids: dict[str, UUID]
) -> None:
    async with maker() as session:
        await session.execute(
            delete(SubmissionValidation).where(
                SubmissionValidation.submission_id.in_(
                    [
                        ids["stale"],
                        ids["fresh"],
                        ids["terminal_history"],
                    ]
                )
            )
        )
        await session.execute(
            delete(Submission).where(
                Submission.id.in_([ids["stale"], ids["fresh"], ids["terminal_history"]])
            )
        )
        await session.execute(
            delete(AssignmentClaim).where(AssignmentClaim.task_id == ids["task"])
        )
        await session.execute(
            delete(Assignment).where(Assignment.task_id == ids["task"])
        )
        await session.execute(delete(Task).where(Task.id == ids["task"]))
        user_ids = [
            value
            for key, value in ids.items()
            if key == "teacher" or key.startswith("student:")
        ]
        await session.execute(delete(User).where(User.id.in_(user_ids)))
        await session.commit()


def test_stale_validating_discovery_judges_the_newest_run_row() -> None:
    """Only the genuinely wedged row qualifies: the stale VALIDATING
    submission whose newest run row predates the threshold. A fresh
    in-flight run never qualifies, and neither does a terminal
    submission merely carrying interrupted-run history."""
    maker = _factory()
    run = uuid4().hex[:8]
    ids = asyncio.run(_seed_world(maker, run))
    try:

        async def _discover() -> list[UUID]:
            async with maker() as session:
                return await collect_stale_validating_ids(
                    session,
                    _NOW,
                    stale_after=timedelta(minutes=30),
                    limit=500,
                )

        discovered = asyncio.run(_discover())
        assert ids["stale"] in discovered
        assert ids["fresh"] not in discovered
        assert ids["terminal_history"] not in discovered

        # The batch ceiling bounds the scan (the shared scan shape).
        async def _limited() -> list[UUID]:
            async with maker() as session:
                return await collect_stale_validating_ids(
                    session,
                    _NOW,
                    stale_after=timedelta(minutes=30),
                    limit=0,
                )

        assert asyncio.run(_limited()) == []
    finally:
        asyncio.run(_cleanup_world(maker, ids))


# --- family 2 discovery: stale UPLOADED (final pass B P1) ----------------------------


def test_stale_uploaded_discovery_judges_submitted_at_against_the_grace() -> None:
    """Only UPLOADED rows past the dispatch grace qualify: one seeded
    five minutes old (grace 120s — the stranded shape), one seeded ten
    seconds ago (a healthy finalize whose enqueue is merely in flight).
    Neither carries a run row — UPLOADED rows never do — so
    ``submitted_at`` is the family's whole signal."""
    maker = _factory()
    run = uuid4().hex[:8]
    ids = asyncio.run(_seed_uploaded_world(maker, run))
    try:

        async def _discover() -> list[UUID]:
            async with maker() as session:
                return await collect_stale_uploaded_ids(
                    session,
                    _NOW,
                    grace=timedelta(seconds=120),
                    limit=500,
                )

        discovered = asyncio.run(_discover())
        assert ids["stranded"] in discovered
        assert ids["fresh"] not in discovered
    finally:
        asyncio.run(_cleanup_uploaded_world(maker, ids))


# --- the task-level recovery: the owner-named three ----------------------------------


@dataclass(slots=True)
class RecoveryWorld:
    """The claim-side graph the recovery flows build on."""

    teacher_id: UUID
    student_id: UUID
    task_id: UUID
    claim_id: UUID


class _ExplodingDispatcher:
    """A ValidationDispatcher whose enqueue fails — the broker-outage
    shape right after finalize's commit (family 2a)."""

    def enqueue_validation(self, submission_id: UUID, request_id: str) -> None:
        raise RuntimeError("simulated broker outage at the validation enqueue")


def _session_ctx(factory: async_sessionmaker[AsyncSession]):
    """The injected session-source shape the validation job calls:
    ``session_source()`` inside its own ``asyncio.run``."""

    def _ctx() -> AsyncSession:
        return factory()

    return _ctx


@contextmanager
def _eager_celery_app():
    """TEST-ONLY eager app (the test_validate_submission_job_e2e
    pattern): built from real settings, default-set so the shared_task
    proxies bind, job modules imported exactly the way the worker CLI
    imports them. The workers-suite conftest restores the Celery app
    slots after every test; this additionally restores them around the
    eager block so later code in the SAME test sees no app drift."""
    import celery._state as celery_state

    from app.core.config import get_settings
    from app.workers.celery_app import create_celery_app

    previous_current = getattr(celery_state._tls, "current_app", None)
    previous_default = celery_state.default_app
    app = create_celery_app(get_settings())
    app.conf.task_always_eager = True
    app.set_default()
    app.loader.import_default_modules()
    try:
        yield app
    finally:
        celery_state.default_app = previous_default
        celery_state._tls.current_app = previous_current


def _run_scan_task(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    storage: FakeObjectStorage,
    *,
    mine: set[UUID],
) -> dict[str, Any]:
    """Run the REAL scan task eagerly against the REAL database.

    The scan's session source runs on this test's NullPool factory, and
    the re-dispatched validation job gets the fake storage holding the
    uploaded object plus the same factory (the e2e pattern). The two
    collectors are wrapped — REAL predicate first, then intersected
    with THIS test's rows — because the integration database is shared
    and a leftover stale row from an aborted earlier run must never be
    eagerly validated against this test's storage fake (the discovery
    predicates themselves are pinned unfiltered above)."""
    from app.workers.jobs import requeue_stale_validating as requeue_module
    from app.workers.jobs import validate_submission as job_module

    async def _session_run(work: Any) -> Any:
        async with factory() as session:
            return await work(session)

    real_validating = requeue_module.collect_stale_validating_ids
    real_uploaded = requeue_module.collect_stale_uploaded_ids

    async def _filtered_validating(
        session: AsyncSession, now: datetime, *, stale_after: timedelta, limit: int
    ) -> list[UUID]:
        discovered = await real_validating(
            session, now, stale_after=stale_after, limit=limit
        )
        return [row_id for row_id in discovered if row_id in mine]

    async def _filtered_uploaded(
        session: AsyncSession, now: datetime, *, grace: timedelta, limit: int
    ) -> list[UUID]:
        discovered = await real_uploaded(session, now, grace=grace, limit=limit)
        return [row_id for row_id in discovered if row_id in mine]

    monkeypatch.setattr(requeue_module, "run_with_session", _session_run)
    monkeypatch.setattr(
        requeue_module, "collect_stale_validating_ids", _filtered_validating
    )
    monkeypatch.setattr(
        requeue_module, "collect_stale_uploaded_ids", _filtered_uploaded
    )
    monkeypatch.setattr(job_module, "_default_storage", lambda: storage)
    monkeypatch.setattr(
        job_module, "_default_session_source", lambda: _session_ctx(factory)
    )
    with _eager_celery_app():
        return requeue_module.requeue_stale_validating.delay("req-requeue-scan").get(
            timeout=120
        )


def test_scan_redispatches_upload_stranded_by_a_failed_publish_and_validation_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner-named family 2a: finalize's COMMIT succeeded but the
    validation dispatch after it raised (a broker outage; the client
    never retries upload-complete) — the row sits UPLOADED with no run
    row, invisible to the stale-VALIDATING family. The scan re-dispatches
    it and the REAL validation job (eager) drives it to VALIDATED,
    chained reward lock included."""
    from app.modules.submissions.upload_service import UploadService

    maker = _factory()
    run = uuid4().hex[:8]
    world = asyncio.run(_seed_recovery_world(maker, run))
    try:
        clock = FrozenClock(_NOW)
        storage = FakeObjectStorage(clock=clock)

        async def _finalize() -> Submission:
            async with maker() as session:
                service = UploadService(
                    clock=clock, storage=storage, dispatcher=_ExplodingDispatcher()
                )
                receipt = await service.create_upload_intent(
                    session,
                    Actor(user_id=world.student_id, role=Role.STUDENT),
                    world.claim_id,
                    "数据.csv",
                    "CSV",
                    len(_VALID_CSV),
                )
                storage.put_object(object_key=receipt.object_key, content=_VALID_CSV)
                return await service.finalize_upload(
                    session,
                    Actor(user_id=world.student_id, role=Role.STUDENT),
                    receipt.intent_id,
                )

        # The dispatch failure surfaces AFTER the commit: finalize raises
        # while the Submission row is already durable.
        with pytest.raises(RuntimeError, match="simulated broker outage"):
            asyncio.run(_finalize())

        stranded = asyncio.run(_latest_row(maker, world.claim_id))
        assert stranded is not None
        assert stranded.validation_status == "UPLOADED"
        assert asyncio.run(_run_rows(maker, world.claim_id)) == []

        # The scan discovers it (submitted_at is a frozen instant far
        # past the 120s default grace) and the eager re-dispatch runs
        # the real validation job to a terminal state.
        payload = _run_scan_task(monkeypatch, maker, storage, mine={stranded.id})
        assert payload["stale_uploaded"] == 1
        assert payload["submission_ids"] == [str(stranded.id)]

        healed = asyncio.run(_latest_row(maker, world.claim_id))
        assert healed is not None
        assert healed.validation_status == "VALIDATED"
        assert healed.validation_report is not None
        # The chained reward lock ran inside the real job: the claim
        # left VALIDATING for UNDER_REVIEW.
        claim = asyncio.run(_claim_state(maker, world.claim_id))
        assert claim.status == ClaimStatus.UNDER_REVIEW.value
        assert claim.reward_lock_status == RewardLockStatus.PROVISIONAL.value
    finally:
        asyncio.run(_cleanup_recovery(maker, world))


def test_scan_heals_upload_stranded_by_exhausted_cleanup_claim_conflict_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner-named family 2b: an old version's retention deletion holds
    a live cleanup claim, so the fresh upload's validation tx1 keeps
    refusing with CleanupClaimConflictError — Celery's bounded ladder
    (~31s of backoff) exhausts long inside the 300s lease, the job
    fails loudly, and the row sits UPLOADED with the whole tx1 rolled
    back: no run row, invisible to the stale-VALIDATING family. Once
    the deletion completes, the scan re-dispatches and the row heals."""
    from app.modules.submissions.cleanup_claim import CleanupClaimConflictError
    from app.modules.submissions.upload_service import UploadService
    from app.modules.submissions.validation_runner import (
        SandboxLimits,
        ValidatorSandbox,
    )
    from app.modules.submissions.validation_service import ValidationService

    maker = _factory()
    run = uuid4().hex[:8]
    world = asyncio.run(_seed_recovery_world(maker, run, seed_old_due=True))
    try:
        clock = FrozenClock(_NOW)
        storage = FakeObjectStorage(clock=clock)

        async def _finalize_v2() -> Submission:
            async with maker() as session:
                service = UploadService(clock=clock, storage=storage)
                receipt = await service.create_upload_intent(
                    session,
                    Actor(user_id=world.student_id, role=Role.STUDENT),
                    world.claim_id,
                    "数据v2.csv",
                    "CSV",
                    len(_VALID_CSV),
                )
                storage.put_object(object_key=receipt.object_key, content=_VALID_CSV)
                return await service.finalize_upload(
                    session,
                    Actor(user_id=world.student_id, role=Role.STUDENT),
                    receipt.intent_id,
                )

        v2 = asyncio.run(_finalize_v2())
        assert v2.version == 2
        assert v2.validation_status == "UPLOADED"

        # The validation service (what the job's ladder replays): every
        # attempt refuses inside tx1 and rolls back whole. Repeated
        # refusals stand in for the exhausted Celery retries — the end
        # state is identical: UPLOADED, no run row, claim still CLAIMED,
        # nothing wedged in VALIDATING.
        service = ValidationService(
            clock=clock,
            storage=storage,
            sandbox=ValidatorSandbox(
                limits=SandboxLimits(
                    wall_timeout_seconds=60.0,
                    memory_limit_bytes=1024 * 1024 * 1024,
                    cpu_seconds=60,
                )
            ),
        )

        def _attempt() -> None:
            async def _call() -> None:
                async with maker() as session:
                    await service.validate_submission(session, v2.id)

            asyncio.run(_call())

        for _ in range(3):
            with pytest.raises(CleanupClaimConflictError):
                _attempt()
        stranded = asyncio.run(_row_by_version(maker, world.claim_id, version=2))
        assert stranded is not None
        assert stranded.validation_status == "UPLOADED"
        assert asyncio.run(_run_rows(maker, world.claim_id)) == []
        claim = asyncio.run(_claim_state(maker, world.claim_id))
        assert claim.status == ClaimStatus.CLAIMED.value

        # The deletion completes (the seconds-scale claim window
        # closing); protection no longer refuses.
        asyncio.run(_complete_old_deletion(maker, world.claim_id))

        payload = _run_scan_task(monkeypatch, maker, storage, mine={v2.id})
        assert payload["stale_uploaded"] == 1
        assert payload["submission_ids"] == [str(v2.id)]

        healed = asyncio.run(_row_by_version(maker, world.claim_id, version=2))
        assert healed is not None
        assert healed.validation_status == "VALIDATED"
    finally:
        asyncio.run(_cleanup_recovery(maker, world))


def test_scan_leaves_fresh_uploaded_rows_below_the_grace_undispatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner-named threshold semantics: a row finalized seconds ago —
    its validation enqueue merely in flight — is BELOW the dispatch
    grace and the scan must not touch it (zero dispatch; no hot loop
    over healthy finalizes)."""
    maker = _factory()
    run = uuid4().hex[:8]
    ids = asyncio.run(_seed_uploaded_world(maker, run, fresh_only=True))
    fresh_id = ids["fresh"]
    storage = FakeObjectStorage()
    try:
        payload = _run_scan_task(monkeypatch, maker, storage, mine={fresh_id})
        assert payload["stale_uploaded"] == 0
        assert payload["requeued"] == 0
        assert str(fresh_id) not in payload["submission_ids"]

        row = asyncio.run(_row_by_id(maker, fresh_id))
        assert row is not None
        assert row.validation_status == "UPLOADED"
        assert asyncio.run(_run_rows_by_id(maker, fresh_id)) == []
    finally:
        asyncio.run(_cleanup_uploaded_world(maker, ids))


# --- shared seeding / inspection helpers ----------------------------------------------


async def _seed_uploaded_world(
    maker: async_sessionmaker[AsyncSession],
    run: str,
    *,
    fresh_only: bool = False,
) -> dict[str, UUID]:
    """UPLOADED submissions on one task, one per student/claim:

    - ``stranded``: submitted five minutes before the frozen judge
      instant — past a 120s grace, the family-2 wedge shape;
    - ``fresh``: submitted ten seconds before the judge instant (with
      ``fresh_only``, seeded against the REAL clock for the task-level
      zero-dispatch test) — a healthy finalize whose enqueue is merely
      in flight.
    """
    fresh_submitted = (
        datetime.now(UTC) - timedelta(seconds=10)
        if fresh_only
        else _NOW - timedelta(seconds=10)
    )
    specs: list[tuple[str, datetime]] = (
        [("fresh", fresh_submitted)]
        if fresh_only
        else [
            ("stranded", _NOW - timedelta(minutes=5)),
            ("fresh", fresh_submitted),
        ]
    )
    ids: dict[str, UUID] = {}
    async with maker() as session:
        teacher = User(
            username=f"t{run}",
            password_hash=_PASSWORD_HASH,
            nickname=f"老师{run[-4:]}",
            phone_e164=None,
            role=Role.TEACHER,
            status=UserStatus.ACTIVE,
        )
        session.add(teacher)
        await session.flush()
        students: dict[str, User] = {}
        for index, (name, _) in enumerate(specs):
            student = User(
                username=f"2025{run}0{index + 1}",
                password_hash=_PASSWORD_HASH,
                nickname=f"同学{run[-4:]}",
                phone_e164=None,
                role=Role.STUDENT,
                status=UserStatus.ACTIVE,
            )
            session.add(student)
            students[name] = student
        await session.flush()
        task = Task(
            owner_teacher_id=teacher.id,
            title="校园自习室使用情况调查",
            description="采集自习室数据。",
            task_type=TaskType.DATA_CRAWL,
            rarity=TaskRarity.NORMAL,
            base_reward_points=100,
            status=TaskStatus.PUBLISHED,
            deadline_mode=DeadlineMode.RELATIVE,
            duration_minutes=4320,
            submission_schema=_CSV_SCHEMA,
            submission_schema_version=1,
            allowed_file_types=["CSV"],
            max_file_size_bytes=10 * 1024 * 1024,
            notification_channels=["SMS"],
        )
        session.add(task)
        await session.flush()
        for name, submitted_at in specs:
            assignment = Assignment(
                task_id=task.id,
                platform="xiaohongshu",
                keyword=f"自习{name}",
                availability_status=AssignmentAvailability.OCCUPIED,
            )
            session.add(assignment)
            await session.flush()
            claim = AssignmentClaim(
                assignment_id=assignment.id,
                task_id=task.id,
                user_id=students[name].id,
                status=ClaimStatus.CLAIMED.value,
                claimed_at=submitted_at - timedelta(days=3),
                deadline_at=_GRACE - timedelta(minutes=1440),
                grace_deadline_at=_GRACE,
                reward_policy_snapshot={"version": 1},
                base_reward_points_snapshot=100,
                submission_schema_version=1,
                reward_lock_status=RewardLockStatus.NONE,
            )
            session.add(claim)
            await session.flush()
            submission = Submission(
                claim_id=claim.id,
                version=1,
                object_key=f"submissions/{claim.id}/{uuid4()}",
                original_filename="数据.csv",
                declared_type="CSV",
                file_size=len(_VALID_CSV),
                submitted_at=submitted_at,
                validation_status="UPLOADED",
                retention_until=_NOW + timedelta(days=180),
            )
            session.add(submission)
            await session.flush()
            claim.latest_submission_id = submission.id
            ids[name] = submission.id
        await session.commit()
        ids["task"] = task.id
        ids["teacher"] = teacher.id
        ids.update(
            {f"student:{student.username}": student.id for student in students.values()}
        )
        return ids


async def _cleanup_uploaded_world(
    maker: async_sessionmaker[AsyncSession], ids: dict[str, UUID]
) -> None:
    submission_ids = [ids[key] for key in ("stranded", "fresh") if key in ids]
    async with maker() as session:
        await session.execute(
            delete(SubmissionValidation).where(
                SubmissionValidation.submission_id.in_(submission_ids)
            )
        )
        await session.execute(
            delete(Submission).where(Submission.id.in_(submission_ids))
        )
        await session.execute(
            delete(AssignmentClaim).where(AssignmentClaim.task_id == ids["task"])
        )
        await session.execute(
            delete(Assignment).where(Assignment.task_id == ids["task"])
        )
        await session.execute(delete(Task).where(Task.id == ids["task"]))
        user_ids = [
            value
            for key, value in ids.items()
            if key == "teacher" or key.startswith("student:")
        ]
        await session.execute(delete(User).where(User.id.in_(user_ids)))
        await session.commit()


async def _seed_recovery_world(
    maker: async_sessionmaker[AsyncSession],
    run: str,
    *,
    seed_old_due: bool = False,
) -> RecoveryWorld:
    """Teacher + student + task + assignment + a CLAIMED claim. With
    ``seed_old_due``: also version 1, a long-failed old submission whose
    retention has lapsed and whose deletion a cleanup worker has CLAIMED
    (lease NULL — fail-safe live while unset), the family-2b blocker."""
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
            title="校园食堂满意度问卷采集",
            description="采集问卷数据。",
            task_type=TaskType.DATA_CRAWL,
            rarity=TaskRarity.NORMAL,
            base_reward_points=100,
            status=TaskStatus.PUBLISHED,
            deadline_mode=DeadlineMode.RELATIVE,
            duration_minutes=4320,
            submission_schema=_CSV_SCHEMA,
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
            keyword=f"食堂{run[-4:]}",
            availability_status=AssignmentAvailability.OCCUPIED,
        )
        session.add(assignment)
        await session.flush()
        claim = AssignmentClaim(
            assignment_id=assignment.id,
            task_id=task.id,
            user_id=student.id,
            status=ClaimStatus.CLAIMED.value,
            claimed_at=_NOW - timedelta(days=3),
            deadline_at=_GRACE - timedelta(minutes=1440),
            grace_deadline_at=_GRACE,
            reward_policy_snapshot={"version": 1},
            base_reward_points_snapshot=100,
            submission_schema_version=1,
            reward_lock_status=RewardLockStatus.NONE,
        )
        session.add(claim)
        await session.flush()
        if seed_old_due:
            old = Submission(
                claim_id=claim.id,
                version=1,
                object_key=f"submissions/{claim.id}/old-{uuid4()}",
                original_filename="数据.csv",
                declared_type="CSV",
                file_size=128,
                submitted_at=_NOW - timedelta(days=60),
                validation_status="VALIDATION_FAILED",
                retention_until=_NOW - timedelta(days=30),
                cleanup_claimed_at=_NOW - timedelta(seconds=2),
            )
            session.add(old)
            await session.flush()
            claim.latest_submission_id = old.id
        await session.commit()
        return RecoveryWorld(
            teacher_id=teacher.id,
            student_id=student.id,
            task_id=task.id,
            claim_id=claim.id,
        )


async def _cleanup_recovery(
    maker: async_sessionmaker[AsyncSession], world: RecoveryWorld
) -> None:
    from app.modules.notifications.models import Notification, NotificationDelivery
    from app.modules.submissions.models import RewardLockHistory, UploadIntent

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
            delete(AssignmentClaim).where(AssignmentClaim.task_id == world.task_id)
        )
        await session.execute(
            delete(Assignment).where(Assignment.task_id == world.task_id)
        )
        await session.execute(delete(Task).where(Task.id == world.task_id))
        # Defensive: a failure-path notification (none on VALIDATED, but
        # cheap to clear) — deliveries reference notifications, which
        # reference users.
        await session.execute(
            delete(NotificationDelivery).where(
                NotificationDelivery.user_id == world.student_id
            )
        )
        await session.execute(
            delete(Notification).where(Notification.user_id == world.student_id)
        )
        await session.execute(
            delete(User).where(User.id.in_([world.teacher_id, world.student_id]))
        )
        await session.commit()


async def _latest_row(
    maker: async_sessionmaker[AsyncSession], claim_id: UUID
) -> Submission | None:
    async with maker() as session:
        return await session.scalar(
            select(Submission)
            .where(Submission.claim_id == claim_id)
            .order_by(Submission.version.desc())
            .limit(1)
        )


async def _row_by_version(
    maker: async_sessionmaker[AsyncSession], claim_id: UUID, *, version: int
) -> Submission | None:
    async with maker() as session:
        return await session.scalar(
            select(Submission).where(
                Submission.claim_id == claim_id, Submission.version == version
            )
        )


async def _row_by_id(
    maker: async_sessionmaker[AsyncSession], submission_id: UUID
) -> Submission | None:
    async with maker() as session:
        return await session.get(Submission, submission_id)


async def _run_rows(
    maker: async_sessionmaker[AsyncSession], claim_id: UUID
) -> list[str]:
    async with maker() as session:
        rows = (
            await session.scalars(
                select(SubmissionValidation.status)
                .where(
                    SubmissionValidation.submission_id.in_(
                        select(Submission.id).where(Submission.claim_id == claim_id)
                    )
                )
                .order_by(SubmissionValidation.started_at)
            )
        ).all()
        return list(rows)


async def _run_rows_by_id(
    maker: async_sessionmaker[AsyncSession], submission_id: UUID
) -> list[str]:
    async with maker() as session:
        rows = (
            await session.scalars(
                select(SubmissionValidation.status).where(
                    SubmissionValidation.submission_id == submission_id
                )
            )
        ).all()
        return list(rows)


async def _claim_state(
    maker: async_sessionmaker[AsyncSession], claim_id: UUID
) -> AssignmentClaim:
    async with maker() as session:
        claim = await session.get(AssignmentClaim, claim_id)
        assert claim is not None
        return claim


async def _complete_old_deletion(
    maker: async_sessionmaker[AsyncSession], claim_id: UUID
) -> None:
    """The cleanup worker finishes: deleted_at set, the protection
    guard's conflict condition lifts."""
    async with maker() as session:
        await session.execute(
            update(Submission)
            .where(
                Submission.claim_id == claim_id,
                Submission.cleanup_claimed_at.is_not(None),
            )
            .values(deleted_at=_NOW)
        )
        await session.commit()
