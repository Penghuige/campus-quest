# backend/tests/integration/submissions/test_submission_constraints.py
"""Database-level submission constraints (spec §10-13, §31.11).

Service code checks these first for friendly errors; these tests prove
PostgreSQL itself rejects the violations even when the application forgets:

- UNIQUE(claim_id, version) on submissions: Submission version is unique
  per Claim (§31.11). Version numbering is per-Claim, so the same integer
  under another Claim inserts cleanly; monotonic allocation is a service
  duty (plan 04 task 2), the database only guarantees uniqueness;
- UNIQUE(object_key): the server-generated storage key is globally unique
  (spec §10: keys are generated server-side, never from the original
  filename);
- server defaults: validation_status UPLOADED and review_status
  PENDING_REVIEW (frozen initial members, interfaces.md), legal_hold
  false, retention_permanent false (spec §13: permanent retention is an
  explicit flag);
- retention is exactly one of a retention_until snapshot or the explicit
  permanent flag — never both, never neither. A "huge sentinel date"
  standing in for permanence is unrepresentable (spec §13);
- closed value sets (VARCHAR + CHECK) reject unknown validation/review
  statuses and file types;
- review and reward-lock history rows are append-only by convention: the
  tables declare no onupdate columns and later service tasks expose no
  UPDATE path, because spec §11.2 forbids overwriting INVALIDATED audit
  history. The database deliberately does not enforce append-only.

Rows are built after their parents flush: ids are server-generated, so a
transient parent's id is still None at construction time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.submissions.enums import (
    ReviewAction,
    ReviewStatus,
    RewardLockStatus,
    ValidationStatus,
)
from app.modules.submissions.models import (
    RewardLockHistory,
    Submission,
    SubmissionReview,
    SubmissionValidation,
)
from app.modules.tasks.enums import (
    AssignmentAvailability,
    ClaimStatus,
    DeadlineMode,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.models import Assignment, AssignmentClaim, Task

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)

# Sentinel distinguishing "retention_until not supplied" (use the dated
# default) from an explicit None (permanent-only or invalid-both cases).
_UNSET: object = object()


def _user(*, username: str, role: Role) -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname="测试同学",
        phone_e164=None,
        role=role,
        status=UserStatus.ACTIVE,
    )


def _teacher(username: str = "teacher0001") -> User:
    return _user(username=username, role=Role.TEACHER)


def _student(username: str = "20250010001") -> User:
    return _user(username=username, role=Role.STUDENT)


def _task(owner: User) -> Task:
    return Task(
        owner_teacher_id=owner.id,
        title="小红书考研经验帖数据采集",
        description="采集指定关键词下的笔记正文与互动数据。",
        task_type=TaskType.DATA_CRAWL,
        rarity=TaskRarity.NORMAL,
        base_reward_points=100,
        status=TaskStatus.PUBLISHED,
        deadline_mode=DeadlineMode.RELATIVE,
        duration_minutes=4320,
        allowed_file_types=["CSV", "XLSX", "SQLITE"],
        max_file_size_bytes=200 * 1024 * 1024,
        notification_channels=["SMS"],
    )


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
    status: ClaimStatus = ClaimStatus.VALIDATING,
) -> AssignmentClaim:
    deadline = datetime.now(UTC) + timedelta(days=3)
    return AssignmentClaim(
        assignment_id=assignment.id,
        task_id=assignment.task_id,
        user_id=student.id,
        status=status,
        deadline_at=deadline,
        grace_deadline_at=deadline + timedelta(minutes=1440),
        reward_policy_snapshot={"version": 1},
        base_reward_points_snapshot=100,
        submission_schema_version=1,
        reward_lock_status=RewardLockStatus.NONE,
    )


def _submission(
    claim: AssignmentClaim,
    *,
    version: int = 1,
    object_key: str | None = None,
    declared_type: str = "CSV",
    retention_until: object = _UNSET,
    retention_permanent: bool | None = None,
    **overrides: Any,
) -> Submission:
    fields: dict[str, Any] = {
        "claim_id": claim.id,
        "version": version,
        # Server-generated key shape (spec §10): claim-scoped path, never
        # the client filename.
        "object_key": object_key
        or f"submissions/{claim.id}/v{version}/0000000000000001.csv",
        "original_filename": "考研英语笔记.csv",
        "declared_type": declared_type,
        "file_size": 1024,
        "submitted_at": datetime.now(UTC),
    }
    # Spec §13: snapshot or explicit permanent flag; one of the two is
    # always supplied by the upload finalize service.
    if retention_until is not _UNSET:
        fields["retention_until"] = retention_until
    elif retention_permanent is not True:
        fields["retention_until"] = datetime.now(UTC) + timedelta(days=180)
    if retention_permanent is not None:
        fields["retention_permanent"] = retention_permanent
    fields.update(overrides)
    return Submission(**fields)


async def _flush(db_session: AsyncSession, *objects: Any) -> None:
    db_session.add_all(objects)
    await db_session.flush()


@pytest.mark.integration
async def test_duplicate_claim_version_rejected(
    db_session: AsyncSession,
) -> None:
    """(claim_id, version) is unique per Claim (spec §31.11)."""
    owner = _teacher()
    student = _student()
    await _flush(db_session, owner, student)
    task = _task(owner)
    await _flush(db_session, task)
    assignment = _assignment(task)
    await _flush(db_session, assignment)
    claim = _claim(assignment, student)
    await _flush(db_session, claim)

    await _flush(db_session, _submission(claim, version=1))

    db_session.add(
        _submission(
            claim,
            version=1,
            object_key="submissions/other/key/v1/0000000000000002.csv",
        )
    )
    with pytest.raises(IntegrityError, match="uq_submissions_claim_id_version"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_same_version_across_claims_allowed(
    db_session: AsyncSession,
) -> None:
    """Version uniqueness is scoped to the Claim (spec §31.11): two claims
    each start their own version sequence at 1."""
    owner = _teacher()
    student_a = _student("20250010001")
    student_b = _student("20250010002")
    await _flush(db_session, owner, student_a, student_b)
    task = _task(owner)
    await _flush(db_session, task)
    assignment_a = _assignment(task, keyword="考研英语")
    assignment_b = _assignment(task, keyword="考研政治")
    await _flush(db_session, assignment_a, assignment_b)
    claim_a = _claim(assignment_a, student_a)
    claim_b = _claim(assignment_b, student_b)
    await _flush(db_session, claim_a, claim_b)

    await _flush(
        db_session,
        _submission(claim_a, version=1),
        _submission(claim_b, version=1),
    )


@pytest.mark.integration
async def test_duplicate_object_key_rejected(
    db_session: AsyncSession,
) -> None:
    """The storage object key is globally unique (spec §10: server
    generated, so two submissions can never share one stored object)."""
    owner = _teacher()
    student = _student()
    await _flush(db_session, owner, student)
    task = _task(owner)
    await _flush(db_session, task)
    assignment = _assignment(task)
    await _flush(db_session, assignment)
    claim = _claim(assignment, student)
    await _flush(db_session, claim)

    shared_key = f"submissions/{claim.id}/shared/key.csv"
    await _flush(db_session, _submission(claim, version=1, object_key=shared_key))

    db_session.add(_submission(claim, version=2, object_key=shared_key))
    with pytest.raises(IntegrityError, match="uq_submissions_object_key"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_submission_defaults_present(
    db_session: AsyncSession,
) -> None:
    """A submission created without status or retention-flag inputs gets
    the frozen initial states: UPLOADED, PENDING_REVIEW (interfaces.md),
    legal_hold false, retention_permanent false (spec §13: permanence is
    an explicit flag, never the default)."""
    owner = _teacher()
    student = _student()
    await _flush(db_session, owner, student)
    task = _task(owner)
    await _flush(db_session, task)
    assignment = _assignment(task)
    await _flush(db_session, assignment)
    claim = _claim(assignment, student)
    await _flush(db_session, claim)

    submission = _submission(claim)
    await _flush(db_session, submission)
    db_session.expunge_all()

    loaded = await db_session.scalar(
        select(Submission).where(Submission.id == submission.id)
    )
    assert loaded is not None
    assert loaded.validation_status == ValidationStatus.UPLOADED
    assert loaded.review_status == ReviewStatus.PENDING_REVIEW
    assert loaded.legal_hold is False
    assert loaded.retention_permanent is False
    assert loaded.created_at is not None
    assert loaded.deleted_at is None
    assert loaded.detected_type is None


@pytest.mark.integration
@pytest.mark.parametrize(
    ("retention_until_offset_days", "retention_permanent", "valid"),
    [
        (180, False, True),  # dated snapshot (spec §13 default DAYS_180)
        (None, True, True),  # explicit permanent flag
        (None, False, False),  # undecided retention is unrepresentable
        (180, True, False),  # both set is ambiguous; permanent is flag-only
    ],
)
async def test_retention_is_exactly_one_of_snapshot_or_permanent(
    db_session: AsyncSession,
    retention_until_offset_days: int | None,
    retention_permanent: bool,
    valid: bool,
) -> None:
    """Spec §13: every submission carries either a retention_until
    snapshot or the explicit permanent flag — never both, never neither —
    so "permanent" can never be faked with a huge sentinel date."""
    owner = _teacher()
    student = _student()
    await _flush(db_session, owner, student)
    task = _task(owner)
    await _flush(db_session, task)
    assignment = _assignment(task)
    await _flush(db_session, assignment)
    claim = _claim(assignment, student)
    await _flush(db_session, claim)

    submission = _submission(
        claim,
        retention_until=(
            datetime.now(UTC) + timedelta(days=retention_until_offset_days)
            if retention_until_offset_days is not None
            else None
        ),
        retention_permanent=retention_permanent,
    )

    if valid:
        await _flush(db_session, submission)
        return

    db_session.add(submission)
    with pytest.raises(IntegrityError, match="ck_submissions_retention_exclusive"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("validation_status", "REJECTED"),
        ("review_status", "PENDING"),
        ("declared_type", "EXE"),
        ("detected_type", "EXE"),
        ("version", 0),
    ],
)
async def test_submission_closed_value_sets_reject_unknown(
    db_session: AsyncSession,
    field: str,
    value: Any,
) -> None:
    """Enum-like columns are VARCHAR + CHECK against the frozen member sets
    (interfaces.md for the statuses, spec §10 for file types): the database
    boundary itself rejects unknown or misspelled values, and versions
    start at 1."""
    owner = _teacher()
    student = _student()
    await _flush(db_session, owner, student)
    task = _task(owner)
    await _flush(db_session, task)
    assignment = _assignment(task)
    await _flush(db_session, assignment)
    claim = _claim(assignment, student)
    await _flush(db_session, claim)

    db_session.add(_submission(claim, **{field: value}))
    with pytest.raises(IntegrityError, match="ck_submissions_"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_validation_review_and_lock_history_rows_persist(
    db_session: AsyncSession,
) -> None:
    """The three history tables hold real rows around one submission: a
    machine-validation run (one row per run, spec §12.4 structured report),
    an immutable review decision, and the reward-lock transition audit
    (spec §11.2: first valid submission locks PROVISIONAL at 100%)."""
    owner = _teacher()
    student = _student()
    await _flush(db_session, owner, student)
    task = _task(owner)
    await _flush(db_session, task)
    assignment = _assignment(task)
    await _flush(db_session, assignment)
    claim = _claim(assignment, student)
    await _flush(db_session, claim)

    submission = _submission(
        claim,
        validation_status=ValidationStatus.VALIDATED,
        detected_type="CSV",
    )
    await _flush(db_session, submission)

    run = SubmissionValidation(
        submission_id=submission.id,
        report={"row_count": 512, "errors": [], "warnings": []},
        parser_version="csv-parser-1",
        started_at=datetime.now(UTC) - timedelta(seconds=4),
        finished_at=datetime.now(UTC),
        duration_ms=4000,
        status=ValidationStatus.VALIDATED,
    )
    review = SubmissionReview(
        submission_id=submission.id,
        reviewer_id=owner.id,
        action=ReviewAction.APPROVE,
        note="数据齐全，通过。",
    )
    lock_event = RewardLockHistory(
        claim_id=claim.id,
        submission_id=submission.id,
        lock_status_from=RewardLockStatus.NONE,
        lock_status_to=RewardLockStatus.PROVISIONAL,
        reward_tier_locked=100,
        locked_reward_points=100,
        changed_by=None,
        reason="首次机器校验通过，锁定奖励档位。",
    )
    await _flush(db_session, run, review, lock_event)
    db_session.expunge_all()

    loaded_run = await db_session.scalar(
        select(SubmissionValidation).where(
            SubmissionValidation.submission_id == submission.id
        )
    )
    loaded_review = await db_session.scalar(
        select(SubmissionReview).where(SubmissionReview.submission_id == submission.id)
    )
    loaded_event = await db_session.scalar(
        select(RewardLockHistory).where(RewardLockHistory.claim_id == claim.id)
    )
    assert loaded_run is not None
    assert loaded_run.status == ValidationStatus.VALIDATED
    assert loaded_run.report is not None and loaded_run.report["row_count"] == 512
    assert loaded_review is not None
    assert loaded_review.action == ReviewAction.APPROVE
    assert loaded_review.created_at is not None
    assert loaded_event is not None
    assert loaded_event.lock_status_from == RewardLockStatus.NONE
    assert loaded_event.lock_status_to == RewardLockStatus.PROVISIONAL


@pytest.mark.integration
async def test_history_action_and_lock_status_closed_sets_reject_unknown(
    db_session: AsyncSession,
) -> None:
    """Review actions (APPROVE/REQUIRE_REVISION/INVALIDATE_LOCK) and
    reward-lock transitions use the frozen RewardLockStatus members at the
    database boundary."""
    owner = _teacher()
    student = _student()
    await _flush(db_session, owner, student)
    task = _task(owner)
    await _flush(db_session, task)
    assignment = _assignment(task)
    await _flush(db_session, assignment)
    claim = _claim(assignment, student)
    await _flush(db_session, claim)
    submission = _submission(claim)
    await _flush(db_session, submission)

    db_session.add(
        SubmissionReview(
            submission_id=submission.id,
            reviewer_id=owner.id,
            action="DELETE",
        )
    )
    with pytest.raises(IntegrityError, match="ck_submission_reviews_action"):
        await db_session.flush()
    await db_session.rollback()

    db_session.add(
        RewardLockHistory(
            claim_id=claim.id,
            submission_id=submission.id,
            lock_status_from=RewardLockStatus.NONE,
            lock_status_to="BROKEN",
            reward_tier_locked=100,
            locked_reward_points=100,
        )
    )
    with pytest.raises(IntegrityError, match="ck_reward_lock_history_lock_status_to"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_history_rows_are_append_only_by_convention() -> None:
    """SubmissionReview and RewardLockHistory are audit history (spec
    §11.2: INVALIDATED transitions must never be overwritten), so the
    models declare no onupdate columns and document append-only semantics;
    the review/lock services of later plan tasks expose INSERT-only paths.
    The database deliberately does not enforce append-only — PostgreSQL
    triggers would put runtime policy into the schema."""
    for model in (SubmissionReview, RewardLockHistory):
        assert model.__doc__ is not None
        assert "append-only" in model.__doc__.lower()
        for column in model.__table__.columns:
            assert column.onupdate is None, (
                f"{model.__tablename__}.{column.name} must not auto-update"
            )
