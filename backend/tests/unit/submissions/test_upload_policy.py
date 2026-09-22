# backend/tests/unit/submissions/test_upload_policy.py
"""Unit tests for the upload policy rules (spec §10 step 2, §9.3, §11.4,
§5.7, §4.1, §13; backend-engineering §4/§21: policy predicates are pure
behavior, no database).

``UploadPolicyService.check`` receives every fact it needs as a planted
input — the claimer's account status, the claim row, the task row, the
declared file facts, and the clock instant — so the whole §10 step-2
checklist ("Backend 校验 Claim 权限、状态、文件类型声明、大小上限和提交
窗口") runs against pure rules with a FrozenClock:

Rejection matrix (the plan-04 task-2 brief minimum list):

- wrong owner                          -> PERMISSION_DENIED (403)
- non-STUDENT role                     -> PERMISSION_DENIED (403), role
                                         judged before status (a suspended
                                         teacher is still the wrong role)
- SUSPENDED/BANNED/PENDING_PHONE       -> ACCOUNT_NOT_ACTIVE (403)
- in-flight claim (VALIDATING/
  UNDER_REVIEW) and terminal claims    -> CLAIM_NOT_SUBMITTABLE (409)
- type outside Task.allowed_file_types -> FILE_TYPE_NOT_ALLOWED (400)
- declared size over the Task limit
  or the deployment-wide cap           -> FILE_TOO_LARGE (400)
- window closed                        -> SUBMISSION_WINDOW_CLOSED (409)

Window boundaries (spec §9.3 "恰好 deadline + 24h：不再接受" and §11.4's
revision window — the "< / <=" discipline §9.3 demands tests for):

    CLAIMED, now = grace - 1ms            -> allowed
    CLAIMED, now = grace exactly          -> SUBMISSION_WINDOW_CLOSED
    REVISION_REQUIRED, past grace but
    before revision_deadline_at           -> allowed (§11.4: review delay
                                             must not cost the student the
                                             revision chance)
    REVISION_REQUIRED, at/past
    revision_deadline_at                  -> closed
    REVISION_REQUIRED without a revision
    deadline                              -> closed (fail-safe)

Retention snapshot (spec §13): DAYS_30/90/180 compute
``submitted_at + N days``; PERMANENT is the explicit flag with a NULL
expiry — never a sentinel date; an unknown policy string is a loud
ValueError, not a silent default.

Filename sanitization (spec §10: display-only metadata, length-capped
and cleaned, never a path): path components are stripped on both
separators, length caps at 255, and an empty result falls back to a
stable placeholder.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.modules.identity.enums import Role, UserStatus
from app.modules.submissions.enums import FileType, RetentionPolicy
from app.modules.submissions.upload_service import (
    FILENAME_FALLBACK,
    MAX_FILENAME_LENGTH,
    SUBMITTABLE_STATUSES,
    UploadPolicyService,
    retention_snapshot,
    sanitize_filename,
    submission_window_open,
)
from app.modules.tasks.claim_service import Claimer
from app.modules.tasks.enums import (
    ClaimStatus,
    DeadlineMode,
    RewardLockStatus,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.models import AssignmentClaim, Task

# --- planted facts ------------------------------------------------------------------

NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
GRACE = NOW + timedelta(minutes=1440)
REVISION_DEADLINE = GRACE + timedelta(hours=48)

STUDENT_ID = uuid4()
CLAIMER = Claimer(id=STUDENT_ID, status=UserStatus.ACTIVE, role=Role.STUDENT)
POLICY = UploadPolicyService()


def _claim(
    user_id: UUID = STUDENT_ID,
    *,
    status: ClaimStatus = ClaimStatus.CLAIMED,
    grace_deadline_at: datetime = GRACE,
    revision_deadline_at: datetime | None = None,
) -> AssignmentClaim:
    return AssignmentClaim(
        assignment_id=uuid4(),
        task_id=uuid4(),
        user_id=user_id,
        status=status,
        claimed_at=NOW - timedelta(days=1),
        deadline_at=grace_deadline_at - timedelta(minutes=1440),
        grace_deadline_at=grace_deadline_at,
        reward_policy_snapshot={"version": 1},
        base_reward_points_snapshot=100,
        submission_schema_version=1,
        reward_lock_status=RewardLockStatus.NONE,
        revision_deadline_at=revision_deadline_at,
    )


def _task(**overrides: Any) -> Task:
    fields: dict[str, Any] = {
        "owner_teacher_id": uuid4(),
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


def _check(
    *,
    claimer: Claimer = CLAIMER,
    claim: AssignmentClaim | None = None,
    task: Task | None = None,
    declared_type: str = FileType.CSV,
    declared_size: int = 4096,
    now: datetime = NOW,
    max_upload_bytes: int = 200 * 1024 * 1024,
) -> None:
    POLICY.check(
        claimer,
        claim if claim is not None else _claim(),
        task if task is not None else _task(),
        declared_type,
        declared_size,
        now,
        max_upload_bytes=max_upload_bytes,
    )


# --- happy path ---------------------------------------------------------------------


def test_in_window_claimed_student_passes_all_gates() -> None:
    """The §10 step-2 checklist head passes silently for the canonical
    case: ACTIVE student, own CLAIMED claim before grace, allowed type,
    size under both caps."""
    assert _check() is None


def test_submittable_statuses_are_the_actionable_pair() -> None:
    """Only CLAIMED and REVISION_REQUIRED accept uploads (spec §8.2's
    actionable pair); the constant the service gates on is exactly that."""
    assert SUBMITTABLE_STATUSES == (
        ClaimStatus.CLAIMED,
        ClaimStatus.REVISION_REQUIRED,
    )


# --- account gate (role, then status) ------------------------------------------------


@pytest.mark.parametrize("role", [Role.TEACHER, Role.ADMIN])
def test_non_student_role_refused(role: Role) -> None:
    """Uploading is a Student capability (spec §4.1): staff roles get
    PERMISSION_DENIED (403) with the upload-specific message."""
    with pytest.raises(Exception) as excinfo:
        _check(claimer=Claimer(id=STUDENT_ID, status=UserStatus.ACTIVE, role=role))
    error = excinfo.value
    assert error.code == "PERMISSION_DENIED"
    assert error.status_code == 403


@pytest.mark.parametrize("role", [Role.TEACHER, Role.ADMIN])
def test_role_gate_precedes_status_gate(role: Role) -> None:
    """A suspended staff account answers PERMISSION_DENIED, not
    ACCOUNT_NOT_ACTIVE — capability before state, the transport guard's
    order (spec §4.1/§5.7)."""
    with pytest.raises(Exception) as excinfo:
        _check(claimer=Claimer(id=STUDENT_ID, status=UserStatus.SUSPENDED, role=role))
    assert excinfo.value.code == "PERMISSION_DENIED"


@pytest.mark.parametrize(
    "status",
    [UserStatus.SUSPENDED, UserStatus.BANNED, UserStatus.PENDING_PHONE],
)
def test_non_active_student_refused(status: UserStatus) -> None:
    """SUSPENDED/BANNED/PENDING_PHONE students cannot submit (spec §5.7):
    ACCOUNT_NOT_ACTIVE (403)."""
    with pytest.raises(Exception) as excinfo:
        _check(claimer=Claimer(id=STUDENT_ID, status=status, role=Role.STUDENT))
    error = excinfo.value
    assert error.code == "ACCOUNT_NOT_ACTIVE"
    assert error.status_code == 403


# --- ownership ----------------------------------------------------------------------


def test_wrong_owner_refused() -> None:
    """The claim belongs to another student: PERMISSION_DENIED (403),
    never a window or type answer."""
    with pytest.raises(Exception) as excinfo:
        _check(claim=_claim(user_id=uuid4()))
    error = excinfo.value
    assert error.code == "PERMISSION_DENIED"
    assert error.status_code == 403


def test_account_gate_precedes_ownership() -> None:
    """A suspended student on someone else's claim answers the account
    gate first (the §10 checklist order: permissions before claim state)."""
    with pytest.raises(Exception) as excinfo:
        _check(
            claimer=Claimer(
                id=STUDENT_ID, status=UserStatus.SUSPENDED, role=Role.STUDENT
            ),
            claim=_claim(user_id=uuid4()),
        )
    assert excinfo.value.code == "ACCOUNT_NOT_ACTIVE"


def test_ownership_precedes_claim_status() -> None:
    """A foreign claim in a non-submittable state is still a permission
    outcome first."""
    with pytest.raises(Exception) as excinfo:
        _check(claim=_claim(user_id=uuid4(), status=ClaimStatus.COMPLETED))
    assert excinfo.value.code == "PERMISSION_DENIED"


# --- claim status gate ---------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        ClaimStatus.VALIDATING,
        ClaimStatus.UNDER_REVIEW,
        ClaimStatus.COMPLETED,
        ClaimStatus.ABANDONED,
        ClaimStatus.EXPIRED,
    ],
)
def test_non_submittable_claim_status_refused(status: ClaimStatus) -> None:
    """VALIDATING/UNDER_REVIEW mean a submission is already in flight and
    the terminal states ended the claim: all are CLAIM_NOT_SUBMITTABLE
    (409) — the registered code, never a 500."""
    with pytest.raises(Exception) as excinfo:
        _check(claim=_claim(status=status))
    error = excinfo.value
    assert error.code == "CLAIM_NOT_SUBMITTABLE"
    assert error.status_code == 409


def test_revision_required_claim_with_future_revision_deadline_passes() -> None:
    """A REVISION_REQUIRED claim inside its revision window is submittable
    (spec §11.4)."""
    assert (
        _check(
            claim=_claim(
                status=ClaimStatus.REVISION_REQUIRED,
                revision_deadline_at=REVISION_DEADLINE,
            )
        )
        is None
    )


# --- file policy --------------------------------------------------------------------


def test_type_outside_task_allowed_set_refused() -> None:
    """The declared type must be in the Task's allowed set (spec §10):
    FILE_TYPE_NOT_ALLOWED (400) with the allowed list in details."""
    with pytest.raises(Exception) as excinfo:
        _check(declared_type=FileType.SQLITE)
    error = excinfo.value
    assert error.code == "FILE_TYPE_NOT_ALLOWED"
    assert error.status_code == 400
    assert error.details["allowed_file_types"] == ["CSV", "XLSX"]


def test_size_over_task_limit_refused() -> None:
    """Declared size above the Task's own cap: FILE_TOO_LARGE (400) with
    the binding limit in details."""
    with pytest.raises(Exception) as excinfo:
        _check(declared_size=10 * 1024 * 1024 + 1)
    error = excinfo.value
    assert error.code == "FILE_TOO_LARGE"
    assert error.status_code == 400
    assert error.details["limit"] == 10 * 1024 * 1024


def test_size_over_deployment_cap_refused() -> None:
    """Spec §10's default 200 MB ceiling applies even when the Task allows
    more; the deployment cap is the binding limit reported."""
    with pytest.raises(Exception) as excinfo:
        _check(
            task=_task(max_file_size_bytes=500 * 1024 * 1024),
            declared_size=300 * 1024 * 1024,
            max_upload_bytes=200 * 1024 * 1024,
        )
    error = excinfo.value
    assert error.code == "FILE_TOO_LARGE"
    assert error.details["limit"] == 200 * 1024 * 1024


def test_type_gate_precedes_window_gate() -> None:
    """Checklist order (spec §10 step 2): file type is judged before the
    submission window — a bad type answers FILE_TYPE_NOT_ALLOWED even
    when the window is also closed."""
    with pytest.raises(Exception) as excinfo:
        _check(
            declared_type=FileType.SQLITE,
            now=GRACE + timedelta(hours=1),
        )
    assert excinfo.value.code == "FILE_TYPE_NOT_ALLOWED"


# --- window boundaries (spec §9.3, §11.4) --------------------------------------------


def test_window_open_one_millisecond_before_grace() -> None:
    """grace - 1ms is still inside the window (§9.3: forbidden only from
    grace onwards)."""
    assert _check(now=GRACE - timedelta(milliseconds=1)) is None


def test_window_closed_exactly_at_grace() -> None:
    """§9.3 "恰好 deadline + 24h：不再接受" — the boundary instant itself
    is closed (strict now < grace)."""
    with pytest.raises(Exception) as excinfo:
        _check(now=GRACE)
    error = excinfo.value
    assert error.code == "SUBMISSION_WINDOW_CLOSED"
    assert error.status_code == 409


def test_window_closed_after_grace_for_claimed() -> None:
    with pytest.raises(Exception) as excinfo:
        _check(now=GRACE + timedelta(hours=1))
    assert excinfo.value.code == "SUBMISSION_WINDOW_CLOSED"


def test_revision_window_allows_past_grace() -> None:
    """REVISION_REQUIRED + future revision_deadline_at: submission stays
    allowed after grace (spec §11.4 — review delay must not eat the
    student's revision chance)."""
    assert (
        _check(
            claim=_claim(
                status=ClaimStatus.REVISION_REQUIRED,
                revision_deadline_at=REVISION_DEADLINE,
            ),
            now=GRACE + timedelta(hours=1),
        )
        is None
    )


def test_revision_window_boundary_instants() -> None:
    claim = _claim(
        status=ClaimStatus.REVISION_REQUIRED,
        revision_deadline_at=REVISION_DEADLINE,
    )
    assert (
        _check(claim=claim, now=REVISION_DEADLINE - timedelta(milliseconds=1)) is None
    )
    with pytest.raises(Exception) as excinfo:
        _check(claim=claim, now=REVISION_DEADLINE)
    assert excinfo.value.code == "SUBMISSION_WINDOW_CLOSED"
    with pytest.raises(Exception) as excinfo:
        _check(claim=claim, now=REVISION_DEADLINE + timedelta(minutes=1))
    assert excinfo.value.code == "SUBMISSION_WINDOW_CLOSED"


def test_revision_required_without_deadline_is_closed() -> None:
    """A REVISION_REQUIRED row with no revision deadline has no revision
    window to re-open: fail safe (closed), never a crash on None."""

    def _naive_check() -> None:
        _check(
            claim=_claim(
                status=ClaimStatus.REVISION_REQUIRED, revision_deadline_at=None
            ),
            now=GRACE + timedelta(hours=1),
        )

    with pytest.raises(Exception) as excinfo:
        _naive_check()
    assert excinfo.value.code == "SUBMISSION_WINDOW_CLOSED"


def test_submission_window_open_predicate_boundaries() -> None:
    """The raw predicate behind the rule: CLAIMED reads grace,
    REVISION_REQUIRED reads the revision deadline; naive deadlines fail
    closed instead of raising on comparison."""
    assert submission_window_open(_claim(), GRACE - timedelta(milliseconds=1))
    assert not submission_window_open(_claim(), GRACE)
    revision = _claim(
        status=ClaimStatus.REVISION_REQUIRED,
        revision_deadline_at=REVISION_DEADLINE,
    )
    assert submission_window_open(revision, GRACE + timedelta(hours=1))
    assert not submission_window_open(
        _claim(status=ClaimStatus.REVISION_REQUIRED, revision_deadline_at=None),
        NOW,
    )
    naive = _claim()
    naive.grace_deadline_at = GRACE.replace(tzinfo=None)  # type: ignore[assignment]
    assert not submission_window_open(naive, NOW)


# --- retention snapshot (spec §13) ---------------------------------------------------


@pytest.mark.parametrize(
    ("policy", "days"),
    [
        (RetentionPolicy.DAYS_30, 30),
        (RetentionPolicy.DAYS_90, 90),
        (RetentionPolicy.DAYS_180, 180),
    ],
)
def test_retention_snapshot_dated_policies(policy: RetentionPolicy, days: int) -> None:
    """DAYS_x snapshots retention_until = submitted_at + x days with the
    permanent flag off."""
    retention_until, permanent = retention_snapshot(policy, NOW)
    assert retention_until == NOW + timedelta(days=days)
    assert permanent is False


def test_retention_snapshot_permanent_is_explicit_flag() -> None:
    """PERMANENT is the explicit flag with a NULL expiry — never a huge
    sentinel date (spec §13)."""
    retention_until, permanent = retention_snapshot(RetentionPolicy.PERMANENT, NOW)
    assert retention_until is None
    assert permanent is True


def test_retention_snapshot_unknown_policy_is_loud() -> None:
    """An unknown policy string raises instead of silently defaulting —
    the CHECK-constrained column makes this unreachable via the database,
    so the ValueError is the direct-caller guard."""
    with pytest.raises(ValueError, match="unknown retention policy"):
        retention_snapshot("DAYS_7", NOW)


# --- filename sanitization (spec §10) ------------------------------------------------


def test_sanitize_filename_strips_path_components() -> None:
    """Only the final segment survives, on both separators — the client
    never gets to steer the stored path-adjacent metadata."""
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("a\\b\\c.csv") == "c.csv"
    assert sanitize_filename("reports/") == FILENAME_FALLBACK


def test_sanitize_filename_caps_length_and_falls_back() -> None:
    assert len(sanitize_filename("长" * 600)) == MAX_FILENAME_LENGTH
    assert sanitize_filename("长" * 600).startswith("长")
    assert sanitize_filename("") == FILENAME_FALLBACK
    assert sanitize_filename("   ") == FILENAME_FALLBACK


def test_sanitize_filename_drops_non_printable_characters() -> None:
    assert sanitize_filename("data\x00.csv") == "data.csv"
    assert sanitize_filename(" data.csv ") == "data.csv"
