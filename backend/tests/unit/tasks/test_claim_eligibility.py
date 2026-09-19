# backend/tests/unit/tasks/test_claim_eligibility.py
"""Unit tests for the claim eligibility checklist (spec §8.2, §8.4, §9.1;
plan 03 task 7; backend-engineering §4/§21: permission predicates are pure
behavior, no database).

``ClaimEligibilityService.check`` receives every fact it needs as a
planted input — the claimer's account status, the task row, the clock
instant, and the claimer's current claims — so the two MUST-test tables
run against pure rules with a FrozenClock:

Cutoff boundary (spec §9.1 "距 deadline 少于 4 小时时停止新领取" — blocked
only when remaining is LESS than the cutoff; §9.3's "avoid divergent
< / <=" rule applies here exactly as it does to reward tiers):

    FIXED remaining 4h01m        -> allowed
    FIXED remaining 4h00m (240m) -> allowed (exactly at the cutoff)
    FIXED remaining 3h59m        -> CLAIM_CUTOFF_REACHED
    custom cutoff (30m)          -> 29m blocked, exactly 30m allowed
    RELATIVE                     -> never cutoff-blocked, however close or
                                   past any stored fixed_deadline_at is
                                   (§9.2: RELATIVE deadlines are computed
                                   per claim, so global clock proximity is
                                   irrelevant)

Quota-status matrix (spec §8.2): a student holds 2 CLAIMED claims plus 1
claim in the parameterized status; the triple blocks a new claim iff
that status is CLAIMED or REVISION_REQUIRED (VALIDATING/UNDER_REVIEW do
not occupy a slot — the student cannot influence review speed — and the
terminal COMPLETED/ABANDONED/EXPIRED never do).

Same-task rule (spec §8.2): any non-terminal claim on the same task ->
TASK_ACTIVE_CLAIM_EXISTS; terminal history and other tasks' active
claims do not block.

Plus the T6-review follow-up: direct unit tests for
``_map_claim_integrity_error`` with synthetic IntegrityErrors — both
partial-index names map to their §8.4 codes (via the driver attribute
or the message), and an unknown constraint maps to None so the caller
re-raises the database failure untouched (backend-engineering §7).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.clock import FrozenClock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.enums import UserStatus
from app.modules.tasks.claim_service import (
    MAX_ACTIVE_CLAIMS,
    ActiveClaim,
    ActiveClaimExistsError,
    ClaimEligibilityService,
    Claimer,
    NoAssignmentAvailableError,
    _map_claim_integrity_error,
)
from app.modules.tasks.enums import ClaimStatus, DeadlineMode, TaskStatus
from app.modules.tasks.models import Task

# --- planted facts ------------------------------------------------------------------

NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
CLOCK = FrozenClock(NOW)

_UQ_ACTIVE_ASSIGNMENT = "uq_assignment_claims_active_assignment"
_UQ_ACTIVE_USER_TASK = "uq_assignment_claims_active_user_task"

# Every claim status exactly once (spec §8.1); the quota matrix runs over
# all of them so a newly added status fails loudly until classified.
ALL_CLAIM_STATUSES = list(ClaimStatus)


def _claimer(*status: UserStatus) -> Claimer:
    return Claimer(id=uuid4(), status=status[0] if status else UserStatus.ACTIVE)


def _task(**overrides: Any) -> Task:
    """A hand-built Task row carrying only the checklist's inputs.

    Defaults describe a healthy FIXED task one week from its deadline
    with the spec §9.1 default cutoff (240 minutes).
    """
    values: dict[str, Any] = {
        "id": uuid4(),
        "owner_teacher_id": uuid4(),
        "status": TaskStatus.PUBLISHED,
        "deadline_mode": DeadlineMode.FIXED,
        "fixed_deadline_at": NOW + timedelta(days=7),
        "duration_minutes": None,
        "claim_cutoff_minutes": 240,
        "submission_schema_version": 2,
    }
    values.update(overrides)
    return Task(**values)


def _claims(
    *statuses: ClaimStatus, task_id: UUID | None = None
) -> tuple[ActiveClaim, ...]:
    """The claimer's current claims, one per status, on distinct tasks
    unless a single ``task_id`` is pinned (the same-task scenarios)."""
    return tuple(
        ActiveClaim(task_id=task_id if task_id is not None else uuid4(), status=status)
        for status in statuses
    )


def _assert_allowed(
    service: ClaimEligibilityService,
    user: Claimer,
    task: Task,
    now: datetime,
    active_claims: tuple[ActiveClaim, ...],
) -> None:
    """Eligible = check returns normally (None); anything raised fails."""
    assert service.check(user, task, now, active_claims=active_claims) is None


def _assert_blocked(
    service: ClaimEligibilityService,
    user: Claimer,
    task: Task,
    now: datetime,
    active_claims: tuple[ActiveClaim, ...],
    code: ErrorCode,
    status_code: int,
) -> BusinessError:
    with pytest.raises(BusinessError) as exc_info:
        service.check(user, task, now, active_claims=active_claims)
    assert exc_info.value.code == code
    assert exc_info.value.status_code == status_code
    return exc_info.value


# --- §9.1: the FIXED cutoff boundary -------------------------------------------------


@pytest.mark.parametrize(
    ("remaining", "blocked"),
    [
        pytest.param(timedelta(hours=4, minutes=1), False, id="4h01m-allowed"),
        pytest.param(timedelta(hours=4), False, id="exactly-240m-allowed"),
        pytest.param(timedelta(hours=3, minutes=59), True, id="3h59m-blocked"),
    ],
)
def test_fixed_cutoff_boundary(remaining: timedelta, blocked: bool) -> None:
    """The brief's exact ladder: blocked only when remaining is strictly
    LESS than claim_cutoff_minutes; exactly-at-cutoff stays claimable."""
    service = ClaimEligibilityService()
    user = _claimer()
    task = _task(fixed_deadline_at=NOW + remaining)
    now = CLOCK.now()

    if blocked:
        error = _assert_blocked(
            service,
            user,
            task,
            now,
            (),
            ErrorCode.CLAIM_CUTOFF_REACHED,
            409,
        )
        assert error.details["claim_cutoff_minutes"] == 240
        assert error.details["fixed_deadline_at"] == (NOW + remaining).isoformat()
    else:
        _assert_allowed(service, user, task, now, ())


@pytest.mark.parametrize(
    ("cutoff_minutes", "remaining", "blocked"),
    [
        pytest.param(30, timedelta(minutes=30), False, id="custom-exact-allowed"),
        pytest.param(30, timedelta(minutes=29), True, id="custom-1m-less-blocked"),
        pytest.param(0, timedelta(0), False, id="zero-cutoff-at-deadline-allowed"),
    ],
)
def test_cutoff_is_configurable(
    cutoff_minutes: int, remaining: timedelta, blocked: bool
) -> None:
    """The cutoff is per-Task configuration (spec §9.1 "cutoff 可配置"),
    not a hardcoded 4 hours; the same strict-less-than boundary applies."""
    service = ClaimEligibilityService()
    task = _task(
        claim_cutoff_minutes=cutoff_minutes,
        fixed_deadline_at=NOW + remaining,
    )

    if blocked:
        _assert_blocked(
            service,
            _claimer(),
            task,
            NOW,
            (),
            ErrorCode.CLAIM_CUTOFF_REACHED,
            409,
        )
    else:
        _assert_allowed(service, _claimer(), task, NOW, ())


@pytest.mark.parametrize(
    "stored_fixed_deadline_at",
    [
        pytest.param(NOW - timedelta(hours=1), id="stored-deadline-long-past"),
        pytest.param(NOW + timedelta(milliseconds=1), id="stored-deadline-1ms-away"),
        pytest.param(None, id="no-stored-deadline"),
    ],
)
def test_relative_deadline_never_cutoff_blocked(
    stored_fixed_deadline_at: datetime | None,
) -> None:
    """RELATIVE tasks compute deadlines per claim (spec §9.2), so the §9.1
    fixed cutoff never engages: even a stored fixed_deadline_at that is
    long past (or 1ms away) does not block the claim."""
    service = ClaimEligibilityService()
    task = _task(
        deadline_mode=DeadlineMode.RELATIVE,
        fixed_deadline_at=stored_fixed_deadline_at,
        duration_minutes=4320,
    )
    _assert_allowed(service, _claimer(), task, CLOCK.now(), ())


# --- §8.2: account and task gates ----------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        pytest.param(UserStatus.PENDING_PHONE, id="pending-phone"),
        pytest.param(UserStatus.SUSPENDED, id="suspended"),
        pytest.param(UserStatus.BANNED, id="banned"),
    ],
)
def test_non_active_account_is_rejected(status: UserStatus) -> None:
    user = _claimer(status)
    error = _assert_blocked(
        ClaimEligibilityService(),
        user,
        _task(),
        NOW,
        (),
        ErrorCode.ACCOUNT_NOT_ACTIVE,
        403,
    )
    assert error.details == {"user_id": str(user.id)}


@pytest.mark.parametrize(
    "status",
    [
        pytest.param(TaskStatus.DRAFT, id="draft"),
        pytest.param(TaskStatus.PAUSED, id="paused"),
        pytest.param(TaskStatus.CLOSED, id="closed"),
        pytest.param(TaskStatus.ARCHIVED, id="archived"),
    ],
)
def test_non_published_task_is_not_claimable(status: TaskStatus) -> None:
    error = _assert_blocked(
        ClaimEligibilityService(),
        _claimer(),
        _task(status=status),
        NOW,
        (),
        ErrorCode.TASK_NOT_CLAIMABLE,
        409,
    )
    assert error.details["task_status"] == status.value
    assert error.details["reason"] == "task_not_published"


def test_published_task_without_schema_version_is_not_claimable() -> None:
    """A PUBLISHED task always carries a schema version; reaching the claim
    without one means publish validation was bypassed (T6 rule, kept)."""
    error = _assert_blocked(
        ClaimEligibilityService(),
        _claimer(),
        _task(submission_schema_version=None),
        NOW,
        (),
        ErrorCode.TASK_NOT_CLAIMABLE,
        409,
    )
    assert error.details["reason"] == "missing_submission_schema_version"


@pytest.mark.parametrize(
    "fixed_deadline_at",
    [
        pytest.param(None, id="missing-deadline"),
        pytest.param(datetime(2026, 1, 15, 9, 0), id="naive-deadline"),
    ],
)
def test_fixed_task_with_unusable_deadline_is_not_claimable(
    fixed_deadline_at: datetime | None,
) -> None:
    """A FIXED deadline that cannot be compared as a UTC instant (spec
    §9.3) reports not-claimable instead of raising (parity with
    TaskService.is_claimable)."""
    error = _assert_blocked(
        ClaimEligibilityService(),
        _claimer(),
        _task(fixed_deadline_at=fixed_deadline_at),
        NOW,
        (),
        ErrorCode.TASK_NOT_CLAIMABLE,
        409,
    )
    assert error.details["reason"] == "invalid_fixed_deadline"


# --- §8.2: the quota-status matrix ---------------------------------------------------


@pytest.mark.parametrize("status", ALL_CLAIM_STATUSES, ids=lambda s: s.value)
def test_quota_status_matrix(status: ClaimStatus) -> None:
    """Holding 2 CLAIMED plus 1 claim in ``status``: the new claim is
    blocked iff ``status`` also occupies a quota slot (CLAIMED and
    REVISION_REQUIRED); every other status leaves a free slot."""
    service = ClaimEligibilityService()
    active_claims = _claims(ClaimStatus.CLAIMED, ClaimStatus.CLAIMED, status)

    if status in (ClaimStatus.CLAIMED, ClaimStatus.REVISION_REQUIRED):
        error = _assert_blocked(
            service,
            _claimer(),
            _task(),
            NOW,
            active_claims,
            ErrorCode.ASSIGNMENT_LIMIT_REACHED,
            409,
        )
        assert error.details == {"limit": MAX_ACTIVE_CLAIMS}
    else:
        _assert_allowed(service, _claimer(), _task(), NOW, active_claims)


def test_all_review_bound_claims_leave_the_quota_free() -> None:
    """VALIDATING/UNDER_REVIEW claims never occupy slots however many
    there are (§8.2: the student cannot influence review speed)."""
    _assert_allowed(
        ClaimEligibilityService(),
        _claimer(),
        _task(),
        NOW,
        _claims(
            ClaimStatus.VALIDATING,
            ClaimStatus.UNDER_REVIEW,
            ClaimStatus.VALIDATING,
        ),
    )


def test_quota_limit_is_injectable() -> None:
    """The limit is a service-level injection point (spec §8.2 keeps V1 at
    3); a tighter limit blocks at the same >= boundary."""
    user, task, now = _claimer(), _task(), NOW
    holding_two = _claims(ClaimStatus.REVISION_REQUIRED, ClaimStatus.REVISION_REQUIRED)

    _assert_allowed(ClaimEligibilityService(), user, task, now, holding_two)
    error = _assert_blocked(
        ClaimEligibilityService(max_active_claims=2),
        user,
        task,
        now,
        holding_two,
        ErrorCode.ASSIGNMENT_LIMIT_REACHED,
        409,
    )
    assert error.details == {"limit": 2}


# --- §8.2: the same-task non-terminal rule -------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        pytest.param(ClaimStatus.CLAIMED, id="claimed"),
        pytest.param(ClaimStatus.VALIDATING, id="validating"),
        pytest.param(ClaimStatus.UNDER_REVIEW, id="under-review"),
        pytest.param(ClaimStatus.REVISION_REQUIRED, id="revision-required"),
    ],
)
def test_same_task_non_terminal_claim_blocks(status: ClaimStatus) -> None:
    task = _task()
    error = _assert_blocked(
        ClaimEligibilityService(),
        _claimer(),
        task,
        NOW,
        _claims(status, task_id=task.id),
        ErrorCode.TASK_ACTIVE_CLAIM_EXISTS,
        409,
    )
    assert error.details == {"task_id": str(task.id)}


@pytest.mark.parametrize(
    "status",
    [
        pytest.param(ClaimStatus.COMPLETED, id="completed"),
        pytest.param(ClaimStatus.ABANDONED, id="abandoned"),
        pytest.param(ClaimStatus.EXPIRED, id="expired"),
    ],
)
def test_same_task_terminal_history_does_not_block(status: ClaimStatus) -> None:
    """V1 allows re-claiming after a terminal claim on the same task
    (spec §8.2) — the per-assignment exclusion is the candidate
    selection's job, not the eligibility checklist's."""
    task = _task()
    _assert_allowed(
        ClaimEligibilityService(),
        _claimer(),
        task,
        NOW,
        _claims(status, task_id=task.id),
    )


def test_other_tasks_claims_do_not_block_this_task() -> None:
    """Active claims on OTHER tasks consume quota but never trigger the
    same-task conflict."""
    _assert_allowed(
        ClaimEligibilityService(),
        _claimer(),
        _task(),
        NOW,
        _claims(ClaimStatus.CLAIMED, ClaimStatus.UNDER_REVIEW),
    )


# --- T6 review follow-up: the integrity backstop -------------------------------------


class _DriverError:
    """Minimal driver-exception stand-in: asyncpg exposes
    ``constraint_name``; psycopg-style errors only carry the constraint
    inside the message text."""

    def __init__(self, constraint_name: str | None = None, message: str = "") -> None:
        self.constraint_name = constraint_name
        self._message = message

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self._message


def _integrity_error(orig: _DriverError) -> IntegrityError:
    return IntegrityError("INSERT INTO assignment_claims (...) VALUES (...)", {}, orig)


_TASK_ID = uuid4()


def test_active_assignment_index_maps_to_no_assignment_available() -> None:
    mapped = _map_claim_integrity_error(
        _integrity_error(_DriverError(constraint_name=_UQ_ACTIVE_ASSIGNMENT)),
        _TASK_ID,
    )
    assert isinstance(mapped, NoAssignmentAvailableError)
    assert mapped.code == ErrorCode.NO_ASSIGNMENT_AVAILABLE
    assert mapped.status_code < 500
    assert mapped.details == {"task_id": str(_TASK_ID)}


def test_active_user_task_index_maps_to_active_claim_exists() -> None:
    mapped = _map_claim_integrity_error(
        _integrity_error(_DriverError(constraint_name=_UQ_ACTIVE_USER_TASK)),
        _TASK_ID,
    )
    assert isinstance(mapped, ActiveClaimExistsError)
    assert mapped.code == ErrorCode.TASK_ACTIVE_CLAIM_EXISTS
    assert mapped.status_code < 500
    assert mapped.details == {"task_id": str(_TASK_ID)}


def test_constraint_name_is_read_from_the_message_when_the_driver_lacks_it() -> None:
    exc = _integrity_error(
        _DriverError(
            message=(
                "duplicate key value violates unique constraint "
                f'"{_UQ_ACTIVE_USER_TASK}"'
            )
        )
    )
    mapped = _map_claim_integrity_error(exc, _TASK_ID)
    assert isinstance(mapped, ActiveClaimExistsError)


@pytest.mark.parametrize(
    "orig",
    [
        pytest.param(
            _DriverError(constraint_name="uq_assignments_task_id_platform_keyword"),
            id="unrelated-unique-constraint",
        ),
        pytest.param(
            _DriverError(message="duplicate key value violates unique constraint"),
            id="constraint-name-not-quoted",
        ),
        pytest.param(_DriverError(message="checksum failure"), id="not-a-conflict"),
        pytest.param(_DriverError(), id="no-information-at-all"),
    ],
)
def test_unknown_constraints_are_not_mapped(orig: _DriverError) -> None:
    """Backend-engineering §7: only the two expected partial-index
    conflicts become business errors; everything else returns None so the
    caller re-raises the database failure untouched."""
    assert _map_claim_integrity_error(_integrity_error(orig), _TASK_ID) is None
