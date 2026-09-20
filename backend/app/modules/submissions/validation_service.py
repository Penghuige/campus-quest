# backend/app/modules/submissions/validation_service.py
"""Asynchronous submission validation and structured report persistence
(spec §10 step 8, §11.1, §12.4, §32; backend-engineering §5, §12, §14;
plan 04 task 7).

``ValidationService.validate_submission`` is the ONE owner of the
machine validation state (spec §8.1: 状态转换必须由服务层集中定义):

    UPLOADED -> VALIDATING -> VALIDATED | VALIDATION_FAILED

Transaction shape — TWO short write transactions around the parse,
because a multi-second parse must never hold the row lock:

- **tx1** (lock + start): ``SELECT submissions ... FOR UPDATE``;
  terminal state (VALIDATED / VALIDATION_FAILED) -> return the
  persisted report WITHOUT re-running or appending history (spec §32
  idempotency — a retried job replays, it never duplicates); UPLOADED
  -> proceed; a stale VALIDATING (a worker died between its two
  transactions) is re-run-safe: proceed with a fresh run row while
  the interrupted row stays as honest history. Set VALIDATING, insert
  the ``submission_validations`` run row (status VALIDATING, report
  NULL, ``parser_version`` "pending" — the column is NOT NULL and the
  real parser version only exists after the run; tx2 overwrites it),
  commit.
- **parse** (NO transaction held): download the object through the
  storage port into a temp file, then execute the matching validator
  inside the sandboxed subprocess (spec §33.3; see
  ``validation_runner``). Storage ``OSError`` / transient adapter
  errors PROPAGATE deliberately — infrastructure is the job's bounded
  Celery retry class, never a property of the file, and the submission
  stays VALIDATING so the retry re-enters through the stale-rerun
  gate.
- **tx2** (lock + finish): re-lock the row (``populate_existing`` so
  a concurrently finished twin run is seen, last-writer-wins — both
  reports describe the same frozen object), persist the full §12.4
  report onto BOTH the submission projection (latest-run copy) and
  the run history row, plus ``detected_type``, final status,
  finished_at / duration_ms. Commit.

Type detection is CONTENT truth (spec §12: fake .csv/.sqlite must
fail by detection), run inside the sandboxed child; this service owns
the policy gates:

1. declared type outside the Task's ``allowed_file_types`` (the
   upload-time gate, re-checked as defense in depth — no parse run);
2. detected content matches none of the three types;
3. detected differs from the declared type (the declaration was a
   lie; the detected truth is still persisted);
4. detected type outside the validation schema's ``allowed_formats``
   (the DSL's validation-time gate).

Every gate failure is a terminal VALIDATION_FAILED report carrying
the single stable code ``FILE_TYPE_NOT_ALLOWED``. A Task schema that
does not parse against the closed DSL fails as ``SCHEMA_INVALID``
(server-side config fault, still a representable outcome). Sandbox
timeout / crash / OOM arrive as the runner's synthetic reports
(``VALIDATION_TIMED_OUT`` / ``VALIDATION_WORKER_CRASHED``) — terminal,
never a hung worker (backend-engineering §14).

The persisted report shape is exactly ``report_to_json`` (§12.4 field
list + ``preview_rows``): bounded sanitized preview rows (plain
string cells, truncated values, cached XLSX values only — formulas
are never evaluated), no object keys, no parser internals.

Scope note (plan boundary): this service sets SUBMISSION state only.
The claim transition on VALIDATED (reward lock, CLAIMED ->
UNDER_REVIEW) is task 8's service, which consumes the VALIDATED
state produced here.

The service never reads the environment: clock, storage port,
sandbox, and preview bounds arrive as constructor dependencies the
composition root wires from Settings (backend-engineering §11/§17).
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.integrations.object_storage import ObjectStorage
from app.modules.submissions.enums import FileType, ValidationStatus
from app.modules.submissions.models import Submission, SubmissionValidation
from app.modules.submissions.schema import SchemaParseError, SubmissionSchema
from app.modules.submissions.validation_runner import (
    SandboxedValidation,
    ValidatorSandbox,
)
from app.modules.submissions.validators.common import (
    PreviewSpec,
    ValidationCode,
    ValidationReport,
    ValidationReportBuilder,
    report_from_json,
    report_to_json,
)
from app.modules.tasks.models import AssignmentClaim, Task

__all__ = [
    "DETECTION_PARSER_VERSION",
    "RUN_STARTED_PARSER_VERSION",
    "SubmissionNotFoundError",
    "ValidationRunResult",
    "ValidationService",
]

#: parser_version of gate-failure reports (detection ran, no format
#: parser did).
DETECTION_PARSER_VERSION = "detect-1"

#: parser_version written to the run row at start; the real parser
#: version (or detect-1 / sandbox-1) overwrites it in the finishing
#: transaction. A row still carrying this value is the fingerprint of
#: an interrupted run.
RUN_STARTED_PARSER_VERSION = "pending"

_SUBMISSION_NOT_FOUND_MESSAGE = "提交记录不存在"

_TERMINAL_STATUSES = (ValidationStatus.VALIDATED, ValidationStatus.VALIDATION_FAILED)


class SubmissionNotFoundError(BusinessError):
    """No submission under the id (or the graph behind it broke)."""

    def __init__(self, submission_id: UUID) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            _SUBMISSION_NOT_FOUND_MESSAGE,
            status_code=404,
            details={"submission_id": str(submission_id)},
        )


@dataclass(frozen=True, slots=True)
class ValidationRunResult:
    """What ``validate_submission`` hands back to the job.

    ``report`` is the §12.4 report of the run that produced the
    current terminal state — fresh on a new run, reconstructed from
    the persisted JSONB on an idempotent replay
    (``already_terminal``). ``run_id`` is None exactly on that replay
    path (no new history row was written).
    """

    report: ValidationReport
    status: ValidationStatus
    detected_type: FileType | None
    run_id: UUID | None
    already_terminal: bool


def _synthetic_report(
    *, parser_version: str, file_type: FileType, code: ValidationCode, message: str
) -> ValidationReport:
    builder = ValidationReportBuilder(
        parser_version=parser_version, file_type=file_type
    )
    builder.add_error(code, message)
    return builder.build(
        row_count=0,
        detected_columns=[],
        missing_required_columns=[],
        extra_columns=[],
        type_error_counts={},
        null_ratios={},
        duplicate_counts={},
        duration_ms=0.0,
    )


class ValidationService:
    """Owns the machine validation state machine and report persistence."""

    def __init__(
        self,
        *,
        clock: Clock,
        storage: ObjectStorage,
        sandbox: ValidatorSandbox,
        preview: PreviewSpec | None = None,
    ) -> None:
        self._clock = clock
        self._storage = storage
        self._sandbox = sandbox
        self._preview = preview

    async def validate_submission(
        self, db: AsyncSession, submission_id: UUID
    ) -> ValidationRunResult:
        """Validate one submission and persist the structured report.

        Raises ``SubmissionNotFoundError`` for a missing row and
        propagates storage-layer transients (``OSError`` and the
        adapter failure taxonomy) for the job's bounded retry; every
        content-level outcome is a terminal state plus report.
        """
        # --- tx1: lock, idempotency gate, VALIDATING, run row ------------
        submission = await self._lock(db, submission_id)
        status = ValidationStatus(submission.validation_status)
        if status in _TERMINAL_STATUSES:
            if submission.validation_report is None:
                # Unreachable while every terminal write also writes a
                # report; fail loudly instead of fabricating one.
                raise RuntimeError(
                    f"submission {submission_id} is {status.value} with no report"
                )
            return ValidationRunResult(
                report=report_from_json(submission.validation_report),
                status=status,
                detected_type=(
                    FileType(submission.detected_type)
                    if submission.detected_type is not None
                    else None
                ),
                run_id=None,
                already_terminal=True,
            )

        started_at = self._clock.now()
        submission.validation_status = ValidationStatus.VALIDATING.value
        run = SubmissionValidation(
            submission_id=submission.id,
            parser_version=RUN_STARTED_PARSER_VERSION,
            status=ValidationStatus.VALIDATING.value,
            started_at=started_at,
        )
        db.add(run)
        await db.flush()  # populate the server-generated run id
        await db.commit()  # tx1 commits: the row lock is RELEASED here

        # --- parse phase: no transaction, no locks ------------------------
        report, detected = await self._execute(
            db, submission_id, declared=FileType(submission.declared_type)
        )

        # --- tx2: lock, terminal state, report ---------------------------
        finished_at = self._clock.now()
        submission = await self._lock(db, submission_id, populate_existing=True)
        terminal = (
            ValidationStatus.VALIDATED
            if report.passed
            else ValidationStatus.VALIDATION_FAILED
        )
        report_json = report_to_json(report)
        submission.validation_status = terminal.value
        submission.validation_report = report_json
        if detected is not None:
            submission.detected_type = detected.value
        finished_run = await db.get(
            SubmissionValidation,
            run.id,
            populate_existing=True,
        )
        assert finished_run is not None  # inserted and committed by this service
        finished_run.parser_version = report.parser_version
        finished_run.status = terminal.value
        finished_run.report = report_json
        finished_run.finished_at = finished_at
        finished_run.duration_ms = int(
            (finished_at - started_at).total_seconds() * 1000
        )
        await db.commit()
        return ValidationRunResult(
            report=report,
            status=terminal,
            detected_type=detected,
            run_id=finished_run.id,
            already_terminal=False,
        )

    # --- the parse phase ------------------------------------------------

    async def _execute(
        self, db: AsyncSession, submission_id: UUID, *, declared: FileType
    ) -> tuple[ValidationReport, FileType | None]:
        """Gates + download + sandboxed validation (no locks held).

        Returns the report and the detected content type (the truth,
        even on gate failures). Storage transients propagate.
        """
        submission = await db.get(Submission, submission_id)
        assert submission is not None  # locked moments ago in tx1
        claim = await db.scalar(
            select(AssignmentClaim).where(AssignmentClaim.id == submission.claim_id)
        )
        if claim is None:
            raise SubmissionNotFoundError(submission_id)
        task = await db.scalar(select(Task).where(Task.id == claim.task_id))
        if task is None:
            raise SubmissionNotFoundError(submission_id)

        raw_schema = dict(task.submission_schema) if task.submission_schema else {}
        try:
            schema = SubmissionSchema.parse(raw_schema)
        except SchemaParseError as exc:
            report = _synthetic_report(
                parser_version=DETECTION_PARSER_VERSION,
                file_type=declared,
                code=ValidationCode.SCHEMA_INVALID,
                message=f"任务的提交校验 schema 无法解析（{exc.key or '结构'}）",
            )
            return report, None

        # Gate 1: the upload-time allowed set, re-checked (the Task may
        # have been narrowed after the intent was issued).
        allowed = list(task.allowed_file_types or [])
        if declared.value not in allowed:
            return (
                _synthetic_report(
                    parser_version=DETECTION_PARSER_VERSION,
                    file_type=declared,
                    code=ValidationCode.FILE_TYPE_NOT_ALLOWED,
                    message="该任务不接受此文件类型",
                ),
                None,
            )

        with tempfile.TemporaryDirectory(prefix="cq-validation-") as tmp:
            local = Path(tmp) / "upload.bin"
            # Worker-context sync port call: this service runs inside a
            # Celery job's dedicated event loop, never a request handler.
            self._storage.download_to_file(
                object_key=submission.object_key, destination=local
            )
            request: dict[str, object] = {"schema": raw_schema}
            if self._preview is not None:
                request["preview"] = {
                    "max_rows": self._preview.max_rows,
                    "max_value_length": self._preview.max_value_length,
                }
            outcome = self._sandbox.run(
                path=local, request=request, declared_type=declared
            )

        return self._assemble(outcome, declared=declared, schema=schema)

    def _assemble(
        self,
        outcome: SandboxedValidation,
        *,
        declared: FileType,
        schema: SubmissionSchema,
    ) -> tuple[ValidationReport, FileType | None]:
        """Policy gates over the sandbox outcome (see module docstring)."""
        detected = outcome.detected_type
        if outcome.failure_code is not None:
            # Sandbox timeout/crash: already a synthetic terminal report.
            assert outcome.report is not None
            return outcome.report, None
        if detected is None:
            return (
                _synthetic_report(
                    parser_version=DETECTION_PARSER_VERSION,
                    file_type=declared,
                    code=ValidationCode.FILE_TYPE_NOT_ALLOWED,
                    message="文件内容无法识别为 CSV、XLSX 或 SQLite 中的任何一种",
                ),
                None,
            )
        if detected is not declared:
            return (
                _synthetic_report(
                    parser_version=DETECTION_PARSER_VERSION,
                    file_type=declared,
                    code=ValidationCode.FILE_TYPE_NOT_ALLOWED,
                    message=f"文件实际内容为 {detected.value}，与声明的 "
                    f"{declared.value} 不一致",
                ),
                detected,
            )
        if detected not in schema.allowed_formats:
            return (
                _synthetic_report(
                    parser_version=DETECTION_PARSER_VERSION,
                    file_type=declared,
                    code=ValidationCode.FILE_TYPE_NOT_ALLOWED,
                    message=f"任务的提交校验 schema 不接受 {detected.value} 格式",
                ),
                detected,
            )
        assert outcome.report is not None  # a detected type always reports
        return outcome.report, detected

    # --- locking ----------------------------------------------------------

    @staticmethod
    async def _lock(
        db: AsyncSession, submission_id: UUID, *, populate_existing: bool = False
    ) -> Submission:
        statement = (
            select(Submission).where(Submission.id == submission_id).with_for_update()
        )
        if populate_existing:
            statement = statement.execution_options(populate_existing=True)
        submission = await db.scalar(statement)
        if submission is None:
            raise SubmissionNotFoundError(submission_id)
        return submission
