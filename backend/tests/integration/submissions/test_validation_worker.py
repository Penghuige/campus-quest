# backend/tests/integration/submissions/test_validation_worker.py
"""Validation worker service + job against real PostgreSQL (spec §10
step 8, §11.1, §12.4, §32, §37.3 worker tests; plan 04 task 7).

Covers the state machine the worker owns:

- UPLOADED -> VALIDATING (locked, run row started) -> VALIDATED /
  VALIDATION_FAILED with the full §12.4 report persisted onto BOTH the
  submission projection and the per-run history row;
- idempotency (spec §32): a second run over a terminal submission
  returns the persisted report and appends NO history row;
- a stale VALIDATING submission (a worker died between its two
  transactions) is re-run-safe: a fresh run row, terminal state;
- actual-type detection (spec §38.4): a fake binary .csv and a fake
  .sqlite (really XLSX bytes) fail FILE_TYPE_NOT_ALLOWED by CONTENT,
  and the detected truth is persisted;
- preview bounds: the persisted report carries at most N sanitized
  rows with truncated values;
- sandbox failures are terminal outcomes, never a hung worker
  (timeout / memory-cap fixtures);
- storage transients propagate for the job's bounded Celery retry and
  leave the run resumable (VALIDATING, not failed);
- the Celery job end-to-end in eager mode (the plan-01 health
  pattern) and its retry classification.

Harness notes (same conventions as test_upload_finalize.py): explicit
committed sessions from a NullPool engine factory — every asyncio.run
phase runs on a fresh event loop, so pooled connections must never be
reused across them; usernames embed a per-run token; every test
removes its rows with committed DELETEs in FK order in ``finally``.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.submissions.enums import FileType, ValidationStatus
from app.modules.submissions.models import (
    RewardLockHistory,
    Submission,
    SubmissionValidation,
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
from tests.fakes.integrations import FakeObjectStorage

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)
_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
_GRACE = _NOW + timedelta(hours=6)

_CSV_SCHEMA = {
    "required_columns": [
        {"name": "url", "type": "string", "unique": True},
        {"name": "title", "type": "string"},
    ]
}

_SANDBOX_FIXTURES = Path(__file__).resolve().parents[2] / "workers" / "sandbox_fixtures"


def _new_factory() -> async_sessionmaker[AsyncSession]:
    """NullPool session factory: fresh connection per checkout, so
    asyncio.run phases on fresh loops never share a pooled connection."""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False)


@dataclass(slots=True)
class Seed:
    teacher: User
    student: User
    task: Task
    assignment: Assignment
    claim: AssignmentClaim
    submission: Submission


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    run: str,
    *,
    declared_type: str = "CSV",
    content: bytes = b"",
    task_schema: dict[str, Any] | None = None,
    allowed_types: list[str] | None = None,
    claim_status: ClaimStatus = ClaimStatus.VALIDATING,
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
            submission_schema=(task_schema if task_schema is not None else _CSV_SCHEMA),
            submission_schema_version=1,
            allowed_file_types=(
                allowed_types if allowed_types is not None else ["CSV", "XLSX"]
            ),
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
            claimed_at=_NOW - timedelta(days=1),
            deadline_at=_GRACE - timedelta(minutes=1440),
            grace_deadline_at=_GRACE,
            reward_policy_snapshot={"version": 1},
            base_reward_points_snapshot=100,
            submission_schema_version=1,
            reward_lock_status=RewardLockStatus.NONE,
            terminal_at=(
                _NOW
                if claim_status
                in (ClaimStatus.COMPLETED, ClaimStatus.ABANDONED, ClaimStatus.EXPIRED)
                else None
            ),
        )
        session.add(claim)
        await session.flush()
        submission = Submission(
            claim_id=claim.id,
            version=1,
            object_key=f"submissions/{claim.id}/{uuid4()}",
            original_filename="数据.csv",
            declared_type=declared_type,
            file_size=len(content),
            submitted_at=_NOW,
            retention_until=_NOW + timedelta(days=180),
        )
        session.add(submission)
        await session.commit()
        return Seed(
            teacher=teacher,
            student=student,
            task=task,
            assignment=assignment,
            claim=claim,
            submission=submission,
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
                delete(SubmissionValidation).where(
                    SubmissionValidation.submission_id.in_(
                        select(Submission.id).where(Submission.claim_id.in_(claim_ids))
                    )
                )
            )
            # The chained reward lock (the job's on_validation_passed
            # step) writes append-only history rows referencing both the
            # claim and the submission — they go before either.
            await session.execute(
                delete(RewardLockHistory).where(
                    RewardLockHistory.claim_id.in_(claim_ids)
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


def _service(
    storage: FakeObjectStorage,
    *,
    entrypoint: list[str] | None = None,
    wall_timeout_seconds: float = 60.0,
    memory_limit_bytes: int = 1024 * 1024 * 1024,
):
    from app.modules.submissions.validation_runner import (
        SandboxLimits,
        ValidatorSandbox,
    )
    from app.modules.submissions.validation_service import (
        ValidationService,
    )
    from app.modules.submissions.validators.common import PreviewSpec

    sandbox = ValidatorSandbox(
        limits=SandboxLimits(
            wall_timeout_seconds=wall_timeout_seconds,
            memory_limit_bytes=memory_limit_bytes,
            cpu_seconds=60,
        ),
        entrypoint=entrypoint,
    )
    return ValidationService(
        clock=FrozenClock(_NOW),
        storage=storage,
        sandbox=sandbox,
        preview=PreviewSpec(max_rows=10, max_value_length=200),
    )


async def _store(
    factory: async_sessionmaker[AsyncSession], seed: Seed, content: bytes
) -> FakeObjectStorage:
    storage = FakeObjectStorage(clock=FrozenClock(_NOW))
    url = storage.create_upload_url(
        claim_id=seed.claim.id, content_type="text/csv", expires_in=timedelta(minutes=5)
    )
    async with factory() as session:
        row = await session.get(Submission, seed.submission.id)
        assert row is not None
        row.object_key = url.object_key
        await session.commit()
    storage.put_object(object_key=url.object_key, content=content)
    seed.submission.object_key = url.object_key
    return storage


def _validate(
    service: Any, factory: async_sessionmaker[AsyncSession], submission_id: UUID
):
    """Run one service call on its own session inside its own loop."""

    async def _call():
        async with factory() as session:
            return await service.validate_submission(session, submission_id)

    return asyncio.run(_call())


def _valid_csv(rows: int = 3, *, long_cell: str | None = None) -> bytes:
    lines = ["url,title"]
    for index in range(rows):
        title = long_cell if (long_cell is not None and index == 0) else f"t{index}"
        lines.append(f"https://r{index}.com,{title}")
    return ("\n".join(lines) + "\n").encode()


def _xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active.append(("url", "title"))  # type: ignore[union-attr]
    workbook.active.append(("https://a.com", "t"))  # type: ignore[union-attr]
    with tempfile.TemporaryDirectory() as tmp:
        payload = Path(tmp) / "workbook.xlsx"
        workbook.save(payload)
        return payload.read_bytes()


def _manifest_only_xlsx() -> bytes:
    """The T5 carry shape: a manifest-valid archive with no workbook
    part — openpyxl answers OSError('File contains no valid workbook
    part'), which the validators re-raise."""
    import io
    import zipfile

    manifest = (
        b'<?xml version="1.0"?><Types xmlns='
        b'"http://schemas.openxmlformats.org/package/2006/content-types"/>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", manifest)
    return buffer.getvalue()


# --- the state machine ----------------------------------------------------------------


@pytest.mark.integration
def test_valid_csv_validates_and_persists_the_full_report() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = asyncio.run(_seed(factory, run, content=_valid_csv(3)))
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        storage = asyncio.run(_store(factory, seed, _valid_csv(3)))
        service = _service(storage)
        result = _validate(service, factory, seed.submission.id)

        assert result.status is ValidationStatus.VALIDATED
        assert result.already_terminal is False
        assert result.detected_type is FileType.CSV
        assert result.report.passed
        assert result.report.row_count == 3
        assert result.report.parser_version == "csv-1"
        assert result.report.preview_rows[0] == ("https://r0.com", "t0")

        async def _inspect() -> None:
            submission = await factory().get(Submission, seed.submission.id)
            assert submission is not None
            assert submission.validation_status == ValidationStatus.VALIDATED.value
            assert submission.detected_type == "CSV"
            report = submission.validation_report
            assert report is not None
            assert set(report) == {
                "parser_version",
                "file_type",
                "row_count",
                "detected_columns",
                "missing_required_columns",
                "extra_columns",
                "type_error_counts",
                "null_ratios",
                "duplicate_counts",
                "warnings",
                "errors",
                "duration_ms",
                "preview_rows",
            }
            assert report["parser_version"] == "csv-1"
            assert report["row_count"] == 3
            assert report["detected_columns"] == ["url", "title"]
            assert report["errors"] == []
            assert len(report["preview_rows"]) == 3
            runs = (
                (
                    await factory().execute(
                        select(SubmissionValidation).where(
                            SubmissionValidation.submission_id == submission.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert [r.status for r in runs] == ["VALIDATED"]
            assert runs[0].parser_version == "csv-1"
            assert runs[0].report == report
            assert runs[0].duration_ms is not None
            assert runs[0].finished_at is not None

        asyncio.run(_inspect())
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


@pytest.mark.integration
def test_second_job_run_is_idempotent_no_duplicate_history() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = asyncio.run(_seed(factory, run, content=_valid_csv()))
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        storage = asyncio.run(_store(factory, seed, _valid_csv()))
        service = _service(storage)

        first = _validate(service, factory, seed.submission.id)
        second = _validate(service, factory, seed.submission.id)

        assert first.status is ValidationStatus.VALIDATED
        assert second.already_terminal is True
        assert second.report == first.report
        assert second.status is ValidationStatus.VALIDATED

        async def _count() -> int:
            result = await factory().execute(
                select(SubmissionValidation).where(
                    SubmissionValidation.submission_id == seed.submission.id
                )
            )
            return len(result.scalars().all())

        assert asyncio.run(_count()) == 1
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


@pytest.mark.integration
def test_failed_validation_is_terminal_and_replays_idempotently() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        # A CSV whose unique url column repeats: VALIDATION_FAILED.
        content = b"url,title\nhttps://a.com,t\nhttps://a.com,t2\n"
        seed = asyncio.run(_seed(factory, run, content=content))
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        storage = asyncio.run(_store(factory, seed, content))
        service = _service(storage)

        first = _validate(service, factory, seed.submission.id)
        assert first.status is ValidationStatus.VALIDATION_FAILED
        assert first.report.passed is False
        assert any(
            error.code.value == "DUPLICATE_VALUE" for error in first.report.errors
        )
        second = _validate(service, factory, seed.submission.id)
        assert second.already_terminal is True
        assert second.report == first.report

        async def _count() -> int:
            result = await factory().execute(
                select(SubmissionValidation).where(
                    SubmissionValidation.submission_id == seed.submission.id
                )
            )
            return len(result.scalars().all())

        assert asyncio.run(_count()) == 1
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


@pytest.mark.integration
def test_stale_validating_submission_is_rerun_safe() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = asyncio.run(_seed(factory, run, content=_valid_csv()))
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        storage = asyncio.run(_store(factory, seed, _valid_csv()))

        # A previous worker died between its two transactions: the
        # submission is stuck VALIDATING with an interrupted run row.
        async def _wedge() -> None:
            async with factory() as session:
                await session.execute(
                    update(Submission)
                    .where(Submission.id == seed.submission.id)
                    .values(validation_status="VALIDATING")
                )
                session.add(
                    SubmissionValidation(
                        submission_id=seed.submission.id,
                        parser_version="pending",
                        status="VALIDATING",
                        started_at=_NOW,
                    )
                )
                await session.commit()

        asyncio.run(_wedge())
        service = _service(storage)
        result = _validate(service, factory, seed.submission.id)
        assert result.status is ValidationStatus.VALIDATED
        assert result.already_terminal is False

        async def _runs() -> list[str]:
            # Deterministic order: the interrupted run is the one that
            # never finished (started_at is tied — both rows carry the
            # frozen clock's instant — and the fresh row's tx2 UPDATE
            # can reorder the heap, so started_at alone is not stable).
            rows = (
                (
                    await factory().execute(
                        select(SubmissionValidation)
                        .where(SubmissionValidation.submission_id == seed.submission.id)
                        .order_by(SubmissionValidation.finished_at.asc().nulls_first())
                    )
                )
                .scalars()
                .all()
            )
            return [row.status for row in rows]

        # The interrupted run stays as honest history; the fresh run
        # completed. Exactly one row per attempt, no duplicates.
        assert asyncio.run(_runs()) == ["VALIDATING", "VALIDATED"]
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


# --- the claim-side intermediate (plan 04 task 8) ------------------------------------


@pytest.mark.integration
def test_validation_start_moves_actionable_claim_to_validating() -> None:
    """tx1 maps the claim out of the student-actionable statuses at
    validation START (spec §8.1/§8.2: VALIDATING no longer occupies one
    of the 3 actionable-claim slots). The reward-lock service (task 8)
    owns the further VALIDATING -> UNDER_REVIEW step."""
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        # The realistic entry state: finalize left the claim CLAIMED and
        # the job is what moves it.
        seed = asyncio.run(
            _seed(factory, run, content=_valid_csv(), claim_status=ClaimStatus.CLAIMED)
        )
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        storage = asyncio.run(_store(factory, seed, _valid_csv()))
        service = _service(storage)
        result = _validate(service, factory, seed.submission.id)
        assert result.status is ValidationStatus.VALIDATED

        async def _claim_status() -> str:
            claim = await factory().get(AssignmentClaim, seed.claim.id)
            assert claim is not None
            return claim.status

        # Moved by tx1 and left there by tx2 — only the reward-lock
        # service (invoked after validation completes) goes further.
        assert asyncio.run(_claim_status()) == "VALIDATING"
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


@pytest.mark.integration
def test_validation_start_leaves_terminal_claim_untouched() -> None:
    """A terminal claim (the expiry worker won between finalize and this
    job) is never resurrected by validation: the submission still
    validates — machine validation is content truth — but the claim
    stays EXPIRED for the reward-lock service to no-op on."""
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = asyncio.run(
            _seed(factory, run, content=_valid_csv(), claim_status=ClaimStatus.EXPIRED)
        )
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        storage = asyncio.run(_store(factory, seed, _valid_csv()))
        service = _service(storage)
        result = _validate(service, factory, seed.submission.id)
        assert result.status is ValidationStatus.VALIDATED

        async def _claim_status() -> str:
            claim = await factory().get(AssignmentClaim, seed.claim.id)
            assert claim is not None
            return claim.status

        assert asyncio.run(_claim_status()) == "EXPIRED"
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


# --- detection (spec §38.4 fake-file rows) -------------------------------------------


@pytest.mark.integration
def test_fake_binary_csv_fails_file_type_not_allowed() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        content = b"\x7fELF\x02\x01\x01\x00" + bytes(range(256))
        seed = asyncio.run(_seed(factory, run, declared_type="CSV", content=content))
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        storage = asyncio.run(_store(factory, seed, content))
        service = _service(storage)
        result = _validate(service, factory, seed.submission.id)
        assert result.status is ValidationStatus.VALIDATION_FAILED
        assert result.detected_type is None
        assert [error.code.value for error in result.report.errors] == [
            "FILE_TYPE_NOT_ALLOWED"
        ]

        async def _inspect() -> None:
            submission = await factory().get(Submission, seed.submission.id)
            assert submission is not None
            assert submission.validation_status == "VALIDATION_FAILED"
            assert submission.detected_type is None

        asyncio.run(_inspect())
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


@pytest.mark.integration
def test_declared_sqlite_with_xlsx_bytes_fails_and_persists_the_truth() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        content = _xlsx_bytes()
        seed = asyncio.run(
            _seed(
                factory,
                run,
                declared_type="SQLITE",
                content=content,
                allowed_types=["CSV", "XLSX", "SQLITE"],
            )
        )
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        storage = asyncio.run(_store(factory, seed, content))
        service = _service(storage)
        result = _validate(service, factory, seed.submission.id)
        assert result.status is ValidationStatus.VALIDATION_FAILED
        assert result.detected_type is FileType.XLSX
        assert [error.code.value for error in result.report.errors] == [
            "FILE_TYPE_NOT_ALLOWED"
        ]

        async def _inspect() -> None:
            submission = await factory().get(Submission, seed.submission.id)
            assert submission is not None
            assert submission.detected_type == "XLSX"

        asyncio.run(_inspect())
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


@pytest.mark.integration
def test_schema_format_gate_fails_type_the_task_schema_rejects() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        # The upload gate allowed CSV, but the validation-time schema
        # (the DSL's allowed_formats) accepts only sqlite.
        seed = asyncio.run(
            _seed(
                factory,
                run,
                content=_valid_csv(),
                task_schema={"allowed_formats": ["sqlite"], **_CSV_SCHEMA},
            )
        )
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        storage = asyncio.run(_store(factory, seed, _valid_csv()))
        service = _service(storage)
        result = _validate(service, factory, seed.submission.id)
        assert result.status is ValidationStatus.VALIDATION_FAILED
        assert result.detected_type is FileType.CSV
        assert [error.code.value for error in result.report.errors] == [
            "FILE_TYPE_NOT_ALLOWED"
        ]
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


@pytest.mark.integration
def test_invalid_task_schema_fails_with_schema_invalid() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = asyncio.run(
            _seed(
                factory,
                run,
                content=_valid_csv(),
                task_schema={"bogus_key": True, **_CSV_SCHEMA},
            )
        )
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        storage = asyncio.run(_store(factory, seed, _valid_csv()))
        service = _service(storage)
        result = _validate(service, factory, seed.submission.id)
        assert result.status is ValidationStatus.VALIDATION_FAILED
        assert [error.code.value for error in result.report.errors] == [
            "SCHEMA_INVALID"
        ]
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


@pytest.mark.integration
def test_openpyxl_oserror_shape_fails_malformed_not_crashed() -> None:
    """F1 regression: the T5 carry shape — a manifest-valid XLSX archive
    with no workbook part makes openpyxl raise OSError inside the child
    — must persist as a terminal MALFORMED_XLSX validation failure, NOT
    VALIDATION_WORKER_CRASHED (whose retry-later message a terminal
    replay can never deliver)."""
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        content = _manifest_only_xlsx()
        seed = asyncio.run(
            _seed(
                factory,
                run,
                declared_type="XLSX",
                content=content,
                task_schema={"required_columns": [{"name": "url", "type": "string"}]},
            )
        )
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        storage = asyncio.run(_store(factory, seed, content))
        service = _service(storage)

        result = _validate(service, factory, seed.submission.id)
        assert result.status is ValidationStatus.VALIDATION_FAILED
        assert result.detected_type is FileType.XLSX
        assert [error.code.value for error in result.report.errors] == [
            "MALFORMED_XLSX"
        ]

        async def _inspect() -> None:
            submission = await factory().get(Submission, seed.submission.id)
            assert submission is not None
            assert submission.validation_status == "VALIDATION_FAILED"
            report = submission.validation_report
            assert report is not None
            assert report["errors"][0]["code"] == "MALFORMED_XLSX"

        asyncio.run(_inspect())
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


# --- preview bounds ------------------------------------------------------------


@pytest.mark.integration
def test_persisted_preview_is_capped_and_values_truncated() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        long_cell = "x" * 500
        content = _valid_csv(rows=25, long_cell=long_cell)
        seed = asyncio.run(_seed(factory, run, content=content))
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        storage = asyncio.run(_store(factory, seed, content))
        service = _service(storage)
        result = _validate(service, factory, seed.submission.id)
        assert result.status is ValidationStatus.VALIDATED

        async def _inspect() -> None:
            submission = await factory().get(Submission, seed.submission.id)
            assert submission is not None
            report = submission.validation_report
            assert report is not None
            assert report["row_count"] == 25  # exact count...
            assert len(report["preview_rows"]) == 10  # ...capped preview
            assert report["preview_rows"][0][1] == "x" * 200 + "…"

        asyncio.run(_inspect())
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


# --- sandbox failures are outcomes ----------------------------------------------


@pytest.mark.integration
def test_wall_timeout_produces_terminal_failed_report_worker_survives() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = asyncio.run(_seed(factory, run, content=_valid_csv()))
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        storage = asyncio.run(_store(factory, seed, _valid_csv()))
        service = _service(
            storage,
            entrypoint=[sys.executable, str(_SANDBOX_FIXTURES / "sleep_entry.py")],
            wall_timeout_seconds=1.0,
        )

        result = _validate(service, factory, seed.submission.id)
        assert result.status is ValidationStatus.VALIDATION_FAILED
        assert [error.code.value for error in result.report.errors] == [
            "VALIDATION_TIMED_OUT"
        ]

        # The worker (this process) obviously survived; the submission
        # is terminal, not stuck VALIDATING.
        async def _inspect() -> None:
            submission = await factory().get(Submission, seed.submission.id)
            assert submission is not None
            assert submission.validation_status == "VALIDATION_FAILED"

        asyncio.run(_inspect())
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


@pytest.mark.integration
def test_memory_cap_crash_produces_terminal_failed_report() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = asyncio.run(_seed(factory, run, content=_valid_csv()))
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        storage = asyncio.run(_store(factory, seed, _valid_csv()))
        service = _service(
            storage,
            entrypoint=[sys.executable, str(_SANDBOX_FIXTURES / "hog_entry.py")],
            memory_limit_bytes=128 * 1024 * 1024,
        )

        result = _validate(service, factory, seed.submission.id)
        assert result.status is ValidationStatus.VALIDATION_FAILED
        assert [error.code.value for error in result.report.errors] == [
            "VALIDATION_WORKER_CRASHED"
        ]
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


# --- storage transients ----------------------------------------------------------


@pytest.mark.integration
def test_storage_transient_error_propagates_and_leaves_run_resumable() -> None:
    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = asyncio.run(_seed(factory, run, content=_valid_csv()))
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        storage = asyncio.run(_store(factory, seed, _valid_csv()))
        storage.fail_with(OSError("connection reset by peer"))
        service = _service(storage)

        # The transient class propagates for the job's Celery retry —
        # it must NOT become a validation verdict.
        with pytest.raises(OSError):
            _validate(service, factory, seed.submission.id)

        async def _inspect() -> None:
            submission = await factory().get(Submission, seed.submission.id)
            assert submission is not None
            # Stuck-VALIDATING is the resumable state: the next attempt
            # (the retry) re-enters through the stale-rerun-safe gate.
            assert submission.validation_status == "VALIDATING"
            runs = (
                (
                    await factory().execute(
                        select(SubmissionValidation).where(
                            SubmissionValidation.submission_id == submission.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert [r.status for r in runs] == ["VALIDATING"]

        asyncio.run(_inspect())
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


# --- missing anchor ---------------------------------------------------------------


@pytest.mark.integration
def test_missing_submission_raises_typed_not_found() -> None:
    factory = _new_factory()
    storage = FakeObjectStorage()
    service = _service(storage)
    with pytest.raises(BusinessError) as excinfo:
        _validate(service, factory, uuid4())
    assert excinfo.value.code is ErrorCode.NOT_FOUND


# --- the Celery job end-to-end ----------------------------------------------------


@pytest.mark.integration
def test_validate_submission_job_e2e_in_eager_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.workers.jobs import validate_submission as job_module

    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = asyncio.run(_seed(factory, run, content=_valid_csv()))
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        storage = asyncio.run(_store(factory, seed, _valid_csv()))

        with _eager_celery_app() as app:  # noqa: F841  (binding side effect)
            monkeypatch.setattr(job_module, "_default_storage", lambda: storage)
            monkeypatch.setattr(
                job_module, "_default_session_source", lambda: _session_ctx(factory)
            )

            result = job_module.validate_submission_job.delay(
                str(seed.submission.id), "req-e2e-42"
            ).get(timeout=60)

        assert result == {
            "submission_id": str(seed.submission.id),
            "request_id": "req-e2e-42",
            "validation_status": "VALIDATED",
            "passed": True,
            "row_count": 3,
            "parser_version": "csv-1",
            "detected_type": "CSV",
            "already_terminal": False,
        }

        async def _inspect() -> None:
            submission = await factory().get(Submission, seed.submission.id)
            assert submission is not None
            assert submission.validation_status == "VALIDATED"

        asyncio.run(_inspect())
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


@pytest.mark.integration
def test_storage_transient_failure_is_retry_classified_at_the_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.workers.jobs import validate_submission as job_module

    factory = _new_factory()
    run = uuid4().hex[:8]
    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    try:
        seed = asyncio.run(_seed(factory, run, content=_valid_csv()))
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        storage = asyncio.run(_store(factory, seed, _valid_csv()))
        storage.fail_with(OSError("connection reset by peer"), times=99)

        with _eager_celery_app() as app:  # noqa: F841  (binding side effect)
            monkeypatch.setattr(job_module, "_default_storage", lambda: storage)
            monkeypatch.setattr(
                job_module, "_default_session_source", lambda: _session_ctx(factory)
            )

            # The retry classification is pinned on the task itself, and
            # eager execution surfaces the RETRY the job minted for the
            # transient (eager tasks never re-execute inline): the
            # Retry carries the original OSError.
            from celery.exceptions import Retry

            assert OSError in job_module.validate_submission_job.autoretry_for
            with pytest.raises(Retry) as excinfo:
                job_module.validate_submission_job.delay(
                    str(seed.submission.id), "req-retry-1"
                ).get(timeout=60)
            assert isinstance(excinfo.value.exc, OSError)
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


def _session_ctx(factory: async_sessionmaker[AsyncSession]):
    def _ctx():
        return factory()

    return _ctx


def _eager_celery_app():
    """TEST-ONLY eager app (the test_celery_wiring.py pattern): built
    from real settings, default-set so the shared_task proxies bind,
    modules imported exactly the way the worker CLI imports them.

    Constructing a Celery app binds the MAIN THREAD's current-app
    thread-local; without the restore, later files (e.g.
    tests/unit/test_app_wiring.py) would resolve their ``current_app``
    to this test app instead of the one their lifespan installs.
    """
    from contextlib import contextmanager

    import celery._state as celery_state

    from app.workers.celery_app import create_celery_app

    @contextmanager
    def _eager():
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

    return _eager()
