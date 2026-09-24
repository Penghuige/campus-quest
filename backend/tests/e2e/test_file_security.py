# backend/tests/e2e/test_file_security.py
"""Malicious/pathological file gate over the REAL upload chain (plan 10
task 6, steps 2-3).

Every fixture from ``tests/fixtures/files/build_fixtures.py`` rides the
full public pipeline with zero provider overrides (G18):

    seed claim -> presign -> real HTTP PUT to MinIO -> finalize
    -> the REAL ``run_submission_validation`` job (own engine, real
       object-storage download, real sandboxed validator subprocess)

and the gate is the plan's bounded-failure contract, per case:

- the job RETURNS (a JSON summary, never an escaping exception, a
  MemoryError, or a hang — the sandbox/process never dies);
- the submission reaches the terminal ``VALIDATION_FAILED`` state with
  a persisted §12.4 report whose error codes include the case's typed
  expectation (never ``VALIDATION_WORKER_CRASHED``);
- the claim is handed back to the student (the validation service's
  failure back-edge: VALIDATING -> CLAIMED), so a pathological file
  costs a resubmission, never the claim.

The formula-injection case (step 3) pins the display contract: the
server has no formula engine, ``grep -rn "openpyxl" app/`` shows no
spreadsheet export path exists (only the read-only validator), so the
§12.4 report's safe preview IS what students and reviewers see — and
it carries ``= + - @``-prefixed values as literal text.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.main import create_app
from app.modules.submissions.enums import FileType, ValidationStatus
from app.modules.submissions.models import Submission
from app.modules.submissions.validators.common import ValidationCode
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
from app.workers.jobs.validate_submission import run_submission_validation
from tests.e2e.factories import (
    CSV_SUBMISSION_SCHEMA,
    clean_world,
    seed_student,
    seed_teacher_confirmed_totp,
    snapshot_honor_ids,
)
from tests.e2e.test_happy_path import _mint_access_token, _purge_objects, _put
from tests.fixtures.files.build_fixtures import (
    PathologicalFixture,
    binary_renamed_csv,
    fake_sqlite,
    formula_injection_csv,
    high_compression_xlsx,
    oversized_row_count_csv,
    sqlite_multiple_tables,
    very_long_csv_cell,
    xlsx_missing_sheet,
)

pytestmark = pytest.mark.e2e

_BROKER_QUEUE_KEY = "celery"

#: The pathological cases this gate drives (task 6 step 1's seven
#: classes, plus the step-3 formula display case).
_PATHOLOGICAL_CASES = [
    binary_renamed_csv(),
    fake_sqlite(),
    sqlite_multiple_tables(),
    oversized_row_count_csv(),
    very_long_csv_cell(),
    xlsx_missing_sheet(),
    high_compression_xlsx(),
]

_FILE_EXTENSION = {
    FileType.CSV: "csv",
    FileType.XLSX: "xlsx",
    FileType.SQLITE: "sqlite",
}


async def _bearer_headers(
    db_factory: async_sessionmaker[AsyncSession], user_id: UUID
) -> dict[str, str]:
    """Real minted-token bearer headers for one seeded user."""
    return {"Authorization": f"Bearer {await _mint_access_token(db_factory, user_id)}"}


@pytest_asyncio.fixture
async def _stack() -> AsyncIterator[dict[str, Any]]:
    """The real composition stack (the happy-path fixture, unchanged)."""
    settings = get_settings()
    app = create_app()
    broker = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        async with app.router.lifespan_context(app):
            yield {"app": app, "broker": broker, "settings": settings}
    finally:
        with contextlib.suppress(Exception):
            await broker.delete(_BROKER_QUEUE_KEY)
        await broker.aclose()


async def _seed_task(
    factory: async_sessionmaker[AsyncSession],
    *,
    teacher_id: UUID,
    run: str,
    schema: dict[str, Any],
    allowed_file_types: list[str],
    assignment_count: int,
) -> UUID:
    """One PUBLISHED task with a case-specific schema (the factories'
    shape; the local override exists because the pathological cases
    need selector/row-bound schemas the default CSV fixture does not
    carry — the E4 charter keeps factories.py untouched)."""
    async with factory() as db:
        now = datetime.now(UTC)
        task = Task(
            owner_teacher_id=teacher_id,
            title=f"病理文件任务{run[:6]}",
            description="病理上传夹具的目标任务。",
            task_type=TaskType.DATA_CRAWL,
            rarity=TaskRarity.NORMAL,
            base_reward_points=100,
            status=TaskStatus.PUBLISHED,
            deadline_mode=DeadlineMode.RELATIVE,
            duration_minutes=4320,
            submission_schema=schema,
            submission_schema_version=1,
            allowed_file_types=allowed_file_types,
            max_file_size_bytes=10 * 1024 * 1024,
            notification_channels=["SMS"],
            published_at=now - timedelta(days=1),
        )
        db.add(task)
        await db.flush()
        assignments = [
            Assignment(
                task_id=task.id,
                platform="xiaohongshu",
                keyword=f"病理{run[:4]}{index:02d}",
                availability_status=AssignmentAvailability.AVAILABLE,
            )
            for index in range(assignment_count)
        ]
        db.add_all(assignments)
        await db.commit()
        return task.id


async def _seed_claim_at(
    factory: async_sessionmaker[AsyncSession],
    *,
    task_id: UUID,
    student_id: UUID,
    index: int,
) -> UUID:
    """Claim a task's ``index``-th assignment directly through the ORM
    (``factories.seed_claim``'s exact snapshot shape, parameterized by
    assignment so several pathological uploads can share one task)."""
    from sqlalchemy import select

    from app.modules.tasks.claim_service import REWARD_POLICY_SNAPSHOT_V1
    from app.modules.tasks.deadlines import compute_claim_deadlines

    async with factory() as db:
        task = await db.get(Task, task_id)
        assert task is not None
        # Keyword ordering is the deterministic stand-in for the seeding
        # order: the local seeder writes zero-padded run-index keywords
        # ("...00", "...01"), so lexicographic == insertion order for
        # every task this module builds (<= 2 assignments).
        assignment_row = (
            (
                await db.execute(
                    select(Assignment)
                    .where(Assignment.task_id == task_id)
                    .order_by(Assignment.keyword)
                )
            )
            .scalars()
            .all()
        )[index]
        claimed_at = datetime.now(UTC)
        deadlines = compute_claim_deadlines(task, claimed_at)
        claim = AssignmentClaim(
            assignment_id=assignment_row.id,
            task_id=task_id,
            user_id=student_id,
            status=ClaimStatus.CLAIMED,
            claimed_at=claimed_at,
            deadline_at=deadlines.deadline_at,
            grace_deadline_at=deadlines.grace_deadline_at,
            reward_policy_snapshot=dict(REWARD_POLICY_SNAPSHOT_V1),
            base_reward_points_snapshot=task.base_reward_points,
            submission_schema_version=task.submission_schema_version,
            reward_lock_status=RewardLockStatus.NONE,
        )
        assignment_row.availability_status = AssignmentAvailability.OCCUPIED
        db.add(claim)
        await db.commit()
        return claim.id


async def _upload(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    claim_id: UUID,
    fixture: PathologicalFixture,
    run: str,
) -> str:
    """The real presign -> PUT -> finalize chain for one fixture;
    returns the submission id."""
    intent_response = await client.post(
        "/api/v1/submissions/upload-intent",
        headers=headers,
        json={
            "claim_id": str(claim_id),
            "filename": f"病理-{fixture.name}.{_FILE_EXTENSION[fixture.declared_type]}",
            "declared_type": fixture.declared_type.value,
            "size": len(fixture.body),
        },
    )
    assert intent_response.status_code == 201, intent_response.text
    intent = intent_response.json()
    put_status = await asyncio.to_thread(
        _put, intent["upload_url"], fixture.body, intent["headers"], len(fixture.body)
    )
    assert put_status == 200, put_status
    completed = await client.post(
        "/api/v1/submissions/upload-complete",
        headers=headers,
        json={"intent_id": intent["intent_id"]},
    )
    assert completed.status_code == 200, completed.text
    return completed.json()["id"]


async def _validation_codes(
    client: httpx.AsyncClient, headers: dict[str, str], submission_id: str
) -> tuple[set[str], set[str], dict[str, Any]]:
    """The persisted report's error/warning code sets (through the
    student-facing validation endpoint — the same DTO reviewers see)."""
    response = await client.get(
        f"/api/v1/submissions/{submission_id}/validation", headers=headers
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    report = payload["report"]
    assert report is not None, payload
    errors = {finding["code"] for finding in report["errors"]}
    warnings = {finding["code"] for finding in report["warnings"]}
    return errors, warnings, report


async def test_pathological_files_fail_with_bounded_typed_codes(
    db_factory: async_sessionmaker[AsyncSession], _stack: dict[str, Any]
) -> None:
    """Every pathological class rides the real upload chain and lands
    in a terminal, typed, claim-preserving failure — never a crash, a
    MemoryError, or a hang."""
    run = uuid.uuid4().hex[:12]
    honors_before = await snapshot_honor_ids(db_factory)
    teacher = await seed_teacher_confirmed_totp(db_factory, run=run)
    student = await seed_student(db_factory, run=run)
    student_headers = await _bearer_headers(db_factory, student.user_id)

    # One dedicated task per case (the same student may hold only one
    # non-terminal claim per task, so cases never share one).
    def _schema_for(fixture: PathologicalFixture) -> dict[str, Any]:
        schema = dict(CSV_SUBMISSION_SCHEMA)
        schema.update(fixture.schema_overrides)
        return schema

    task_ids: list[UUID] = []
    claim_ids: list[UUID] = []
    try:
        for index, fixture in enumerate(_PATHOLOGICAL_CASES):
            task_id = await _seed_task(
                db_factory,
                teacher_id=teacher.user_id,
                run=f"{run}{index:02d}",
                schema=_schema_for(fixture),
                allowed_file_types=[fixture.declared_type.value],
                assignment_count=1,
            )
            task_ids.append(task_id)
            claim_ids.append(
                await _seed_claim_at(
                    db_factory, task_id=task_id, student_id=student.user_id, index=0
                )
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_stack["app"]),
            base_url="http://e2e-file-security",
        ) as client:
            for fixture, claim_id in zip(_PATHOLOGICAL_CASES, claim_ids, strict=True):
                submission_id = await _upload(
                    client,
                    student_headers,
                    claim_id=claim_id,
                    fixture=fixture,
                    run=run,
                )

                # The REAL job entry: a normal return IS the no-crash
                # proof (an escaping parser error would raise here).
                result = await asyncio.to_thread(
                    run_submission_validation,
                    submission_id,
                    request_id=f"e2e-file-security-{fixture.name}-{run}",
                )

                assert result["validation_status"] == "VALIDATION_FAILED", (
                    fixture.name,
                    result,
                )
                assert result["passed"] is False, (fixture.name, result)
                errors, warnings, _report = await _validation_codes(
                    client, student_headers, submission_id
                )
                expected_errors = {code.value for code in fixture.expected_error_codes}
                expected_warnings = {
                    code.value for code in fixture.expected_warning_codes
                }
                assert expected_errors <= errors, (fixture.name, errors)
                assert expected_warnings <= warnings, (fixture.name, warnings)
                # Typed outcomes only: the sandbox crash code must stay
                # absent (a bounded parser answer, not an OOM/kill).
                assert ValidationCode.VALIDATION_WORKER_CRASHED.value not in errors, (
                    fixture.name,
                    errors,
                )

        # Straight against PostgreSQL: terminal submission state, the
        # persisted report, and the claim handed back to the student.
        async with db_factory() as db:
            from sqlalchemy import select

            for fixture, claim_id in zip(_PATHOLOGICAL_CASES, claim_ids, strict=True):
                claim = await db.get(AssignmentClaim, claim_id)
                assert claim is not None
                assert claim.status == ClaimStatus.CLAIMED.value, (
                    fixture.name,
                    claim.status,
                )
                submission = await db.scalar(
                    select(Submission).where(Submission.claim_id == claim.id)
                )
                assert submission is not None, fixture.name
                assert (
                    submission.validation_status
                    == ValidationStatus.VALIDATION_FAILED.value
                ), (fixture.name, submission.validation_status)
                assert submission.validation_report is not None, fixture.name
    finally:
        await _purge_objects(db_factory, task_ids)
        await clean_world(
            db_factory,
            user_ids=[teacher.user_id, student.user_id],
            task_ids=task_ids,
            honor_ids_before=honors_before,
        )


async def test_formula_injected_values_render_as_literal_text(
    db_factory: async_sessionmaker[AsyncSession], _stack: dict[str, Any]
) -> None:
    """``= + - @``-prefixed cells pass validation and ride the §12.4
    safe preview VERBATIM — the server executes nothing and no export
    path exists to reinterpret them (grep-pinned: ``openpyxl`` appears
    in app/ only inside the read-only validator)."""
    run = uuid.uuid4().hex[:12]
    honors_before = await snapshot_honor_ids(db_factory)
    teacher = await seed_teacher_confirmed_totp(db_factory, run=run)
    student = await seed_student(db_factory, run=run)
    student_headers = await _bearer_headers(db_factory, student.user_id)
    teacher_headers = await _bearer_headers(db_factory, teacher.user_id)
    fixture = formula_injection_csv()
    task_id = await _seed_task(
        db_factory,
        teacher_id=teacher.user_id,
        run=f"{run}f",
        schema=dict(CSV_SUBMISSION_SCHEMA),
        allowed_file_types=["CSV"],
        assignment_count=1,
    )
    try:
        claim_id = await _seed_claim_at(
            db_factory, task_id=task_id, student_id=student.user_id, index=0
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_stack["app"]),
            base_url="http://e2e-file-security",
        ) as client:
            submission_id = await _upload(
                client,
                student_headers,
                claim_id=claim_id,
                fixture=fixture,
                run=run,
            )
            result = await asyncio.to_thread(
                run_submission_validation,
                submission_id,
                request_id=f"e2e-file-security-formula-{run}",
            )
            assert result["validation_status"] == "VALIDATED", result
            assert result["row_count"] == 4, result

            errors, _warnings, report = await _validation_codes(
                client, student_headers, submission_id
            )
            assert errors == set(), errors

            # The display contract: every injection-prefix family
            # appears in the safe preview as its literal text.
            preview_text = "\n".join(
                cell for row in report["preview_rows"] for cell in row
            )
            for literal in (
                "=SUM(A1:A9)",
                "=1+1",
                "+70.1",
                "-2+3",
                "@SUM(A1:A9)",
            ):
                assert literal in preview_text, (literal, preview_text)

        # The teacher's review surface renders the same report shape
        # (no second serialization exists that could execute).
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_stack["app"]),
            base_url="http://e2e-file-security",
        ) as client:
            queue = await client.get(
                "/api/v1/teacher/submissions/review-queue", headers=teacher_headers
            )
            assert queue.status_code == 200, queue.text
            serialized = queue.text
            assert "=1+1" in serialized
            assert "@SUM(A1:A9)" in serialized
    finally:
        await _purge_objects(db_factory, [task_id])
        await clean_world(
            db_factory,
            user_ids=[teacher.user_id, student.user_id],
            task_ids=[task_id],
            honor_ids_before=honors_before,
        )
