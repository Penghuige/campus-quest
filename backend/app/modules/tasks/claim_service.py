# backend/app/modules/tasks/claim_service.py
"""Concurrency-safe random assignment claiming (spec §8.2-8.4, §6.2, §9;
backend-engineering §5-§7).

Transaction shape (spec §8.3, one transaction, one commit at the end):

1. ``SELECT status, role FROM users WHERE id = :user_id FOR UPDATE`` — a
   stable user-level resource is locked FIRST so concurrent claims by the
   same user serialize before any counting (spec §8.3: COUNT-then-INSERT
   alone is unsafe). Chosen over a ClaimQuota row / advisory lock because
   it needs no new table, the row always exists, and it doubles as the
   account gate: BOTH columns are judged on the locked row — status
   (§5.7) and role (§4.1: claiming is a Student capability), so direct
   service callers cannot bypass the transport guard and a concurrent
   role change linearizes behind the same lock.
2. Task gate: the Task row is read ``FOR SHARE`` — concurrent claims
   share the lock freely (claims stay parallel), while a lifecycle
   ``FOR UPDATE`` (pause/close) excludes them, so a PAUSED commit
   linearizes against in-flight claims instead of racing them.
3. Eligibility: ``ClaimEligibilityService.load_active_claims`` fetches
   the claimer's non-terminal claims (the quota and same-task facts)
   still under the user-row lock, then ``check`` runs the whole §8.2
   checklist head — account, task claimability, FIXED cutoff, quota,
   same-task — as pure rules over the locked rows and the clock
   (backend-engineering §4/§21: the predicates stay unit-testable without
   a database because every count arrives as an input). The clock
   instant (``claimed_at``) is sampled immediately BEFORE this step —
   after all locks are held — so lock-wait can never skew the RELATIVE
   deadline anchor, the FIXED cutoff comparison, or the persisted
   claimed_at (see the CLOCK SAMPLING CONTRACT comment in
   ``claim_random_assignment``).
4. Candidate selection: ``... WHERE availability_status = AVAILABLE AND
   id NOT IN (this user's ABANDONED/EXPIRED assignments) ORDER BY
   random() LIMIT 1 FOR UPDATE SKIP LOCKED`` — two transactions can never
   take the same assignment, and a row someone else holds is skipped, not
   waited on.
5. Claim insert + Assignment -> OCCUPIED + snapshot all in the same
   transaction; exactly one ``commit``.

The lock order (user row -> Task FOR SHARE -> assignment locks) is
load-bearing and MUST NOT be reordered.

Design decisions:

- **Cross-module boundary:** interfaces.md forbids importing identity ORM
  models from this module; the lock therefore goes through a typed
  Core-level ``users`` light table reading only ``status`` and ``role``
  (whose value vocabularies are the frozen ``UserStatus`` / ``Role``
  enums — the same import seam collaborator_service uses for ``Role``).
  The port has no locking read; if interfaces.md registers one, this
  query moves behind it.
- **Random strategy:** ``ORDER BY random()`` sorts the candidate set per
  claim — acceptable at V1 volumes and preserves the "user cannot pick a
  specific assignment" semantics. Spec §8.3 MAY pre-generates a
  ``random_key`` column; swapping the ORDER BY expression is the entire
  migration point.
- **Quota statuses:** spec §8.2 counts CLAIMED and REVISION_REQUIRED
  only — VALIDATING/UNDER_REVIEW claims do not occupy a slot (the student
  cannot influence review speed). The (user, task) uniqueness check uses
  all four ACTIVE_CLAIM_STATUSES, matching both partial unique indexes.
- **Errors:** every §8.4 checklist failure raises its stable business
  code with a 4xx status. 409 marks the state-conflict family
  (mirroring deadlines.py's provisional 400; the router docstring owns
  the status table); ACCOUNT_NOT_ACTIVE stays 403 per the identity
  precedent, and a non-STUDENT claimer is PERMISSION_DENIED 403 — a role
  mismatch is a permission outcome, judged role-first so a suspended
  staff account still answers PERMISSION_DENIED (the transport guard's
  capability-then-state order); a missing user or task is 404 NOT_FOUND
  like TaskNotFoundError.
- **IntegrityError mapping:** the two claim partial unique indexes are
  the database backstop for races SKIP LOCKED and the user lock cannot
  produce in practice. Expected violations translate to their §8.4 codes
  (``uq_assignment_claims_active_assignment`` ->
  NO_ASSIGNMENT_AVAILABLE, ``uq_assignment_claims_active_user_task`` ->
  TASK_ACTIVE_CLAIM_EXISTS); any other constraint re-raises untouched —
  unknown database failures must surface, never become conflicts
  (backend-engineering §7).
- **Cutoff boundary:** claiming is blocked when remaining FIXED time is
  strictly LESS than ``claim_cutoff_minutes`` — parity with
  TaskService.is_claimable and the publish gate, so exactly-at-cutoff
  stays claimable on both paths. RELATIVE tasks never hit the fixed
  cutoff at all (spec §9.2: their deadlines are computed per claim).
  Task-level eligibility refinements beyond the checklist (per-task
  restrictions, role gating) arrive with the routes that need them.

Snapshots (spec §6.2, all five MUSTs plus claimed_at) are copied at claim
time and never follow later Task edits:
``base_reward_points_snapshot``, ``deadline_at``/``grace_deadline_at``
(via ``compute_claim_deadlines``, spec §9.1/§9.2),
``reward_policy_snapshot`` (the frozen V1 ladder — fractions as exact
Decimal strings because JSONB cannot carry Decimal and points math never
uses binary floats, §31.1/§31.14), ``submission_schema_version``, and
``claimed_at`` from the injected Clock (backend-engineering §11).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import String, Uuid, column, func, select, table
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import DomainEvent, DomainEventPublisher
from app.modules.tasks.deadlines import compute_claim_deadlines
from app.modules.tasks.enums import (
    AssignmentAvailability,
    ClaimStatus,
    DeadlineMode,
    RewardLockStatus,
    TaskStatus,
)
from app.modules.tasks.models import (
    ACTIVE_CLAIM_STATUSES,
    Assignment,
    AssignmentClaim,
    Task,
)
from app.modules.tasks.service import TaskNotFoundError

__all__ = [
    "CLAIM_EXPIRED",
    "EXPIRY_ACTIONABLE_STATUSES",
    "MAX_ACTIVE_CLAIMS",
    "QUOTA_OCCUPYING_STATUSES",
    "REASSIGN_EXCLUDED_STATUSES",
    "REWARD_POLICY_SNAPSHOT_V1",
    "TERMINAL_CLAIM_STATUSES",
    "REWARD_POLICY_VERSION",
    "AccountNotActiveError",
    "ActiveClaim",
    "ActiveClaimExistsError",
    "AssignmentLimitReachedError",
    "ClaimCutoffReachedError",
    "ClaimEligibilityService",
    "ClaimService",
    "Claimer",
    "ClaimerNotStudentError",
    "ExpireResult",
    "ExpiryOutcome",
    "NoAssignmentAvailableError",
    "NoValidSubmissionsInspector",
    "NotificationEventRecorder",
    "TaskNotClaimableError",
    "UserNotFoundError",
    "ValidSubmissionInspector",
    "effective_expiry_deadline",
]


# --- frozen value sets -----------------------------------------------------------


# spec §8.2: at most 3 claims per student that still need student action.
MAX_ACTIVE_CLAIMS = 3

# Claims that occupy a quota slot (spec §8.2: CLAIMED and
# REVISION_REQUIRED count; VALIDATING/UNDER_REVIEW do not).
QUOTA_OCCUPYING_STATUSES: tuple[ClaimStatus, ...] = (
    ClaimStatus.CLAIMED,
    ClaimStatus.REVISION_REQUIRED,
)

# An assignment handed back by ABANDON/EXPIRE must never be randomly
# re-assigned to the same user (spec §8.2/§8.3 exclusion set).
REASSIGN_EXCLUDED_STATUSES: tuple[ClaimStatus, ...] = (
    ClaimStatus.ABANDONED,
    ClaimStatus.EXPIRED,
)

# Statuses that end the claim lifecycle (the ACTIVE_CLAIM_STATUSES
# complement in models.py); the expiry ladder judges terminality
# against this set, and replaying any terminal claim is a no-op.
TERMINAL_CLAIM_STATUSES: tuple[ClaimStatus, ...] = (
    ClaimStatus.COMPLETED,
    ClaimStatus.ABANDONED,
    ClaimStatus.EXPIRED,
)

# The statuses the expiry worker still acts on (spec §8.2 actionable
# set — the same statuses the quota counts and abandon accepts).
# Claims in the remaining ACTIVE statuses (VALIDATING/UNDER_REVIEW)
# carry an already-submitted file the review pipeline owns.
EXPIRY_ACTIONABLE_STATUSES: tuple[ClaimStatus, ...] = (
    ClaimStatus.CLAIMED,
    ClaimStatus.REVISION_REQUIRED,
)

# Audit-stream identifier for the expiry behavior history — twin of
# CLAIM_ABANDONED in abandon_service (audit contract, deliberately
# NOT a §25 notification event).
CLAIM_EXPIRED = "CLAIM_EXPIRED"

# The V1 reward ladder (spec §9.3) snapshotted onto every claim. Fractions
# are exact Decimal strings: JSONB has no Decimal and points arithmetic
# forbids binary floats (§31.1/§31.14); boundaries are hours past the
# deadline, the final 20% tier running until grace_deadline_at.
REWARD_POLICY_VERSION = 1
REWARD_POLICY_SNAPSHOT_V1: dict[str, Any] = {
    "version": REWARD_POLICY_VERSION,
    "ladder_fractions": ["1", "0.8", "0.5", "0.2"],
    "ladder_boundaries_hours": [0, 4, 12],
}

# Lock/verify seam for the users table (see module docstring): a typed
# Core-level light table, NOT the identity ORM model. Both columns the
# account gate judges — status and role — ride the single locked read.
_USERS_LOCK = table(
    "users",
    column("id", Uuid),
    column("status", String),
    column("role", String),
)

# Partial unique indexes on assignment_claims (models.py) — the integrity
# backstop translated below when a race slips past SKIP LOCKED.
_UQ_ACTIVE_ASSIGNMENT = "uq_assignment_claims_active_assignment"
_UQ_ACTIVE_USER_TASK = "uq_assignment_claims_active_user_task"
_CONSTRAINT_IN_MESSAGE = re.compile(r'constraint "(?P<name>[^"]+)"')


# --- messages (§29 envelope text) --------------------------------------------------


_USER_NOT_FOUND_MESSAGE = "用户不存在"
_ACCOUNT_NOT_ACTIVE_MESSAGE = "账号当前状态不允许执行该操作"
_NOT_STUDENT_MESSAGE = "仅学生账号可领取任务"
_TASK_NOT_CLAIMABLE_MESSAGE = "任务当前不可领取"
_CUTOFF_REACHED_MESSAGE = "距任务截止时间已不足，已停止新领取"
_LIMIT_REACHED_MESSAGE = "当前进行中的任务已达到上限"
_ACTIVE_CLAIM_EXISTS_MESSAGE = "该任务已有进行中的领取，不能重复领取"
_NO_ASSIGNMENT_MESSAGE = "该任务已无可领取的数据单元"


# --- typed exceptions (router-mapped) ---------------------------------------------


class UserNotFoundError(BusinessError):
    """No User row for the id (same shape as TaskNotFoundError)."""

    def __init__(self, user_id: UUID) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            _USER_NOT_FOUND_MESSAGE,
            status_code=404,
            details={"user_id": str(user_id)},
        )


class AccountNotActiveError(BusinessError):
    """The claimer's account is not ACTIVE (spec §8.2; identity 403)."""

    def __init__(self, user_id: UUID) -> None:
        super().__init__(
            ErrorCode.ACCOUNT_NOT_ACTIVE,
            _ACCOUNT_NOT_ACTIVE_MESSAGE,
            status_code=403,
            details={"user_id": str(user_id)},
        )


class ClaimerNotStudentError(BusinessError):
    """The claimer's role is not STUDENT (spec §4.1: claiming is a
    Student capability; Teacher/Admin never enter the claim lifecycle).
    Raised while holding the user-row lock, so direct service callers
    and concurrent role changes cannot bypass it."""

    def __init__(self, user_id: UUID, role: Role) -> None:
        super().__init__(
            ErrorCode.PERMISSION_DENIED,
            _NOT_STUDENT_MESSAGE,
            status_code=403,
            details={"user_id": str(user_id), "role": role.value},
        )


class TaskNotClaimableError(BusinessError):
    """The task is not in a claimable state (not PUBLISHED, or its
    configuration cannot produce a claim)."""

    def __init__(self, status: TaskStatus, *, reason: str) -> None:
        super().__init__(
            ErrorCode.TASK_NOT_CLAIMABLE,
            _TASK_NOT_CLAIMABLE_MESSAGE,
            status_code=409,
            details={"task_status": status.value, "reason": reason},
        )


class ClaimCutoffReachedError(BusinessError):
    """FIXED deadline closer than the claim cutoff (spec §9.1)."""

    def __init__(self, fixed_deadline_at: datetime, claim_cutoff_minutes: int) -> None:
        super().__init__(
            ErrorCode.CLAIM_CUTOFF_REACHED,
            _CUTOFF_REACHED_MESSAGE,
            status_code=409,
            details={
                "fixed_deadline_at": fixed_deadline_at.isoformat(),
                "claim_cutoff_minutes": claim_cutoff_minutes,
            },
        )


class AssignmentLimitReachedError(BusinessError):
    """The student already holds the maximum actionable claims (spec §8.2)."""

    def __init__(self, limit: int) -> None:
        super().__init__(
            ErrorCode.ASSIGNMENT_LIMIT_REACHED,
            _LIMIT_REACHED_MESSAGE,
            status_code=409,
            details={"limit": limit},
        )


class ActiveClaimExistsError(BusinessError):
    """A non-terminal claim already exists for this user on this task."""

    def __init__(self, task_id: UUID) -> None:
        super().__init__(
            ErrorCode.TASK_ACTIVE_CLAIM_EXISTS,
            _ACTIVE_CLAIM_EXISTS_MESSAGE,
            status_code=409,
            details={"task_id": str(task_id)},
        )


class NoAssignmentAvailableError(BusinessError):
    """No candidate assignment is AVAILABLE to this user."""

    def __init__(self, task_id: UUID) -> None:
        super().__init__(
            ErrorCode.NO_ASSIGNMENT_AVAILABLE,
            _NO_ASSIGNMENT_MESSAGE,
            status_code=409,
            details={"task_id": str(task_id)},
        )


# --- integrity backstop ------------------------------------------------------------


def _constraint_name(exc: IntegrityError) -> str | None:
    """The violated constraint's name, from the driver or the message."""
    name = getattr(exc.orig, "constraint_name", None)
    if isinstance(name, str) and name:
        return name
    match = _CONSTRAINT_IN_MESSAGE.search(str(exc.orig))
    return match.group("name") if match is not None else None


def _map_claim_integrity_error(
    exc: IntegrityError, task_id: UUID
) -> BusinessError | None:
    """Translate the two expected partial-index violations into their §8.4
    codes; anything else returns None and re-raises unchanged."""
    name = _constraint_name(exc)
    if name == _UQ_ACTIVE_ASSIGNMENT:
        # Someone else's transaction won this assignment after our read.
        return NoAssignmentAvailableError(task_id)
    if name == _UQ_ACTIVE_USER_TASK:
        # A same-user/same-task claim committed between check and insert.
        return ActiveClaimExistsError(task_id)
    return None


# --- eligibility rules ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Claimer:
    """The claiming user as the eligibility rules see them: the locked row
    reduced to id + status + role, so identity ORM models stay behind the
    module seam (interfaces.md) while ``ClaimEligibilityService.check``
    remains a pure rule over planted inputs."""

    id: UUID
    status: UserStatus
    role: Role


@dataclass(frozen=True, slots=True)
class ActiveClaim:
    """One of the claimer's current claims, reduced to the two columns the
    eligibility rules read (the same-task rule needs the task; the quota
    rule needs only the status)."""

    task_id: UUID
    status: ClaimStatus


class ClaimEligibilityService:
    """The spec §8.2 claim checklist as pure, testable rules.

    ``check`` evaluates every checklist rule that depends only on the
    locked rows, the clock, and the claimer's current claims: account
    role and status, task claimability (PUBLISHED + schema version +
    FIXED cutoff), the global actionable-claim quota, and the same-task
    non-terminal conflict. All facts — counts included — arrive as
    inputs, so the §9.1 cutoff boundary and the §8.2 quota-status matrix
    are unit-testable with a FrozenClock and no database
    (backend-engineering §21); the claim flow calls ``check`` inside its
    locked transaction right after taking the user-row lock (spec §8.3).
    The AVAILABLE-assignment rule is deliberately NOT here: proving one
    exists requires the FOR UPDATE SKIP LOCKED select, which is candidate
    selection, not eligibility.

    Boundary semantics (spec §9.1 "距 deadline 少于 4 小时时停止新领取"):
    blocked only when the remaining FIXED time is strictly LESS than
    ``claim_cutoff_minutes`` — exactly-at-cutoff stays claimable, the
    same edge TaskService.is_claimable and the publish gate use. RELATIVE
    tasks are never cutoff-blocked (§9.2 computes their deadlines per
    claim).

    ``max_active_claims`` is injectable for tests; production wires the
    spec §8.2 default of 3.
    """

    def __init__(self, *, max_active_claims: int = MAX_ACTIVE_CLAIMS) -> None:
        self._max_active_claims = max_active_claims

    def check(
        self,
        user: Claimer,
        task: Task,
        now: datetime,
        *,
        active_claims: Sequence[ActiveClaim],
    ) -> None:
        """Run the §8.2 checklist head; return (None) when eligible.

        Raises the §8.4 business code of the FIRST failed rule, in the
        precedence order: account (role, then status) -> task -> cutoff
        -> quota -> same-task. ``active_claims`` is required (no
        default): the quota and same-task rules are only as strong as
        the facts fed to them.
        """
        self.require_claimable_account(user)
        self._require_claimable_task(task, now)
        self._require_quota_slot(active_claims)
        self._require_no_same_task_claim(task.id, active_claims)

    def require_claimable_account(self, user: Claimer) -> None:
        """Account gate on the locked row (spec §4.1, §5.7, §8.2): role
        STUDENT and status ACTIVE. Role first — a role mismatch is a
        permission outcome (PERMISSION_DENIED) even on a non-ACTIVE
        account, mirroring the transport guard's capability-then-state
        order — then the §5.7 state gate for STUDENT accounts. Exposed
        separately because the claim flow applies it directly at the
        user-row lock, before spending the FOR SHARE task read."""
        if user.role is not Role.STUDENT:
            raise ClaimerNotStudentError(user.id, user.role)
        if user.status is not UserStatus.ACTIVE:
            raise AccountNotActiveError(user.id)

    def _require_claimable_task(self, task: Task, now: datetime) -> None:
        status = TaskStatus(task.status)
        if status is not TaskStatus.PUBLISHED:
            raise TaskNotClaimableError(status, reason="task_not_published")
        if task.submission_schema_version is None:
            # A PUBLISHED task always carries a schema version; reaching
            # here means publish validation was bypassed. The claim-side
            # column is NOT NULL, so refuse instead of failing the insert.
            raise TaskNotClaimableError(
                status, reason="missing_submission_schema_version"
            )
        if DeadlineMode(task.deadline_mode) is DeadlineMode.FIXED:
            self._require_within_cutoff(task, now)

    def _require_within_cutoff(self, task: Task, now: datetime) -> None:
        """FIXED-window gate (spec §9.1): blocked once remaining time is
        strictly less than claim_cutoff_minutes — the same boundary
        TaskService.is_claimable and the publish gate use. A missing or
        naive deadline cannot be compared as an instant and is reported
        as not claimable (mirroring is_claimable)."""
        deadline = task.fixed_deadline_at
        if deadline is None or deadline.tzinfo is None:
            raise TaskNotClaimableError(
                TaskStatus(task.status), reason="invalid_fixed_deadline"
            )
        cutoff = timedelta(minutes=task.claim_cutoff_minutes or 0)
        if deadline.astimezone(UTC) < now + cutoff:
            raise ClaimCutoffReachedError(
                deadline.astimezone(UTC), task.claim_cutoff_minutes or 0
            )

    def _require_quota_slot(self, active_claims: Sequence[ActiveClaim]) -> None:
        occupying = sum(
            1 for claim in active_claims if claim.status in QUOTA_OCCUPYING_STATUSES
        )
        if occupying >= self._max_active_claims:
            raise AssignmentLimitReachedError(self._max_active_claims)

    def _require_no_same_task_claim(
        self, task_id: UUID, active_claims: Sequence[ActiveClaim]
    ) -> None:
        if any(
            claim.task_id == task_id and claim.status in ACTIVE_CLAIM_STATUSES
            for claim in active_claims
        ):
            raise ActiveClaimExistsError(task_id)

    @staticmethod
    async def load_active_claims(
        db: AsyncSession, user_id: UUID
    ) -> tuple[ActiveClaim, ...]:
        """Fetch the quota/same-task facts inside the caller's locked
        transaction (never its own commit): the claimer's non-terminal
        claims reduced to (task_id, status).

        One snapshot feeds both rules — QUOTA_OCCUPYING_STATUSES is a
        subset of ACTIVE_CLAIM_STATUSES, so a single
        ``status IN (ACTIVE)`` fetch loses nothing for either — and the
        count is safe only because every claim by this user holds the
        user-row lock first (spec §8.3).
        """
        rows = (
            await db.execute(
                select(AssignmentClaim.task_id, AssignmentClaim.status).where(
                    AssignmentClaim.user_id == user_id,
                    AssignmentClaim.status.in_(ACTIVE_CLAIM_STATUSES),
                )
            )
        ).all()
        return tuple(
            ActiveClaim(task_id=task_id, status=ClaimStatus(status))
            for task_id, status in rows
        )


# --- notifications seam (interfaces.md cross-module port) --------------------------


class NotificationEventRecorder(Protocol):
    """The duck-typed ``NotificationPort.record_event`` seam
    (interfaces.md "Cross-module ports": persists notification intent
    inside the domain transaction).

    tasks must not import the notifications module (its dependency
    direction is notifications -> identity), so the claim flow declares
    the callable it needs and the composition root injects the concrete
    ``app.modules.notifications.port.NotificationPort``. The default
    (None) keeps ClaimService notification-free — existing callers and
    fakes are unchanged. The accepted event types and payload keys are
    owned by ``app.modules.notifications.event_handlers`` ("CLAIM_CREATED"
    here; canonical NotificationEventType members elsewhere).
    """

    async def record_event(
        self,
        db: AsyncSession,
        event_key: str,
        event_type: str,
        user_id: UUID,
        payload: Mapping[str, Any],
        task_policy: Any = None,
    ) -> None: ...


# --- claim expiry (plan 07 T6; spec §8.2, §11.4, §11.5/§26) --------------------------


def effective_expiry_deadline(claim: AssignmentClaim) -> datetime:
    """The instant the claim becomes expirable: the LATER of the frozen
    grace deadline and, when a review extended the window, the revision
    deadline (max, so an extension always protects and an earlier
    revision deadline never shortens grace)."""
    deadline = claim.grace_deadline_at
    if claim.revision_deadline_at is not None:
        deadline = max(deadline, claim.revision_deadline_at)
    return deadline


class ValidSubmissionInspector(Protocol):
    """Whether the claim's submission state protects it from expiry.

    Amendment-2 seam (plan-04 final review, §11.5/§26 ruling): ONLY a
    machine-VALIDATED submission finalized in-window protects a due
    claim. This branch has no submissions table, so the default
    implementation below answers False and a due claim whose in-window
    submission is not yet VALIDATED expires; the stream that owns
    submission validation wires the real VALIDATED-reading inspector at
    the ClaimService constructor (the expiry worker's
    build_expire_service is the production call site).
    """

    async def has_valid_submission(self, db: AsyncSession, claim_id: UUID) -> bool: ...


class NoValidSubmissionsInspector:
    """The default inspector: no submission protects (answers False).

    Correct on this branch (there is no submission state to read) and
    the pin for the strict amendment-2 reading; replaced — not
    subclassed — by the VALIDATED reader once the submissions module
    exists.
    """

    async def has_valid_submission(self, db: AsyncSession, claim_id: UUID) -> bool:
        return False


class ExpiryOutcome(StrEnum):
    """One expiry attempt's decision (a fact, never an exception)."""

    EXPIRED = "EXPIRED"
    NOT_DUE = "NOT_DUE"
    PROTECTED = "PROTECTED"
    VALID_SUBMISSION = "VALID_SUBMISSION"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    MISSING = "MISSING"


@dataclass(frozen=True, slots=True)
class ExpireResult:
    """The decision plus the claim facts the caller's summary needs,
    captured from the locked row BEFORE the commit (no caller re-reads
    the row to log or serialize the outcome)."""

    claim_id: UUID
    outcome: ExpiryOutcome
    status: ClaimStatus | None
    assignment_id: UUID | None
    task_id: UUID | None
    user_id: UUID | None
    terminal_at: datetime | None


def _expire_result(claim: AssignmentClaim, outcome: ExpiryOutcome) -> ExpireResult:
    """Reduce the locked row to the result snapshot (fields captured
    pre-commit by construction — the caller reads them after the commit
    only from this value)."""
    return ExpireResult(
        claim_id=claim.id,
        outcome=outcome,
        status=ClaimStatus(claim.status),
        assignment_id=claim.assignment_id,
        task_id=claim.task_id,
        user_id=claim.user_id,
        terminal_at=claim.terminal_at,
    )


# --- the service ---------------------------------------------------------------------


class ClaimService:
    """Random assignment claiming under contention (spec §8.2-8.3).

    ``max_active_claims`` is injectable for tests (forwarded to the
    eligibility rules); production wires the spec §8.2 default of 3.
    ``notification_recorder`` (optional) records the claim:...:created
    trigger inside the claim transaction so the notifications module
    plans the deadline reminders in the same commit (plan 07 T5); None
    disables notification scheduling entirely.
    ``event_publisher`` (optional) is the audit seam for CLAIM_EXPIRED
    (plan 07 T6; None disables the audit event); ``valid_submission_inspector``
    (optional, default NoValidSubmissionsInspector) is the amendment-2
    protection seam consulted inside the expiry transaction.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        max_active_claims: int = MAX_ACTIVE_CLAIMS,
        notification_recorder: NotificationEventRecorder | None = None,
        event_publisher: DomainEventPublisher | None = None,
        valid_submission_inspector: ValidSubmissionInspector | None = None,
    ) -> None:
        self._clock = clock
        self._eligibility = ClaimEligibilityService(max_active_claims=max_active_claims)
        self._notification_recorder = notification_recorder
        self._event_publisher = event_publisher
        self._valid_submission_inspector: ValidSubmissionInspector = (
            valid_submission_inspector
            if valid_submission_inspector is not None
            else NoValidSubmissionsInspector()
        )

    async def claim_random_assignment(
        self, db: AsyncSession, user_id: UUID, task_id: UUID
    ) -> AssignmentClaim:
        """Claim one random AVAILABLE assignment of the task for the user.

        Raises the §8.4 business codes (4xx) for every checklist failure;
        commits exactly once, only on the success path.
        """
        # (1) Same-user serialization: lock the stable user-level resource
        # FIRST (spec §8.3), then gate on the row we actually locked —
        # BOTH role and status, before spending the FOR SHARE task read.
        account = (
            await db.execute(
                select(_USERS_LOCK.c.status, _USERS_LOCK.c.role)
                .where(_USERS_LOCK.c.id == user_id)
                .with_for_update()
            )
        ).one_or_none()
        if account is None:
            raise UserNotFoundError(user_id)
        claimer = Claimer(
            id=user_id,
            status=UserStatus(account.status),
            role=Role(account.role),
        )
        self._eligibility.require_claimable_account(claimer)

        # (2) Task gate under FOR SHARE (see module docstring).
        task = await db.scalar(
            select(Task).where(Task.id == task_id).with_for_update(read=True)
        )
        if task is None:
            raise TaskNotFoundError(task_id)

        # CLOCK SAMPLING CONTRACT: ``claimed_at`` is
        # sampled HERE — after every lock the flow takes (user row FOR
        # UPDATE above, Task FOR SHARE above) and before the first
        # eligibility rule that consumes it. Sampling before the locks
        # would let the user-row lock-wait skew the RELATIVE deadline
        # anchor (claimed_at + duration), the FIXED cutoff comparison,
        # and the persisted claimed_at backwards by however long the
        # lock was held — the module charter is exact time. A clock
        # advanced between call start and lock acquisition is therefore
        # invisible by construction (asserted by structure: nothing reads
        # self._clock between method entry and this line). FrozenClock
        # tests pin claimed_at == _NOW on both sides of this contract.
        claimed_at = self._clock.now()

        # (3) The §8.2 checklist head inside the locked transaction:
        # facts still under the user-row lock, then the pure rules —
        # task claimability + FIXED cutoff, the global quota, and the
        # same-task non-terminal conflict.
        active_claims = await ClaimEligibilityService.load_active_claims(db, user_id)
        self._eligibility.check(claimer, task, claimed_at, active_claims=active_claims)

        # (4) Random candidate under FOR UPDATE SKIP LOCKED, excluding
        # assignments this user previously abandoned or expired (§8.2).
        excluded = select(AssignmentClaim.assignment_id).where(
            AssignmentClaim.user_id == user_id,
            AssignmentClaim.status.in_(REASSIGN_EXCLUDED_STATUSES),
        )
        candidate = (
            await db.scalars(
                select(Assignment)
                .where(
                    Assignment.task_id == task_id,
                    Assignment.availability_status == AssignmentAvailability.AVAILABLE,
                    Assignment.id.not_in(excluded),
                )
                .order_by(func.random())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).first()
        if candidate is None:
            raise NoAssignmentAvailableError(task_id)

        # (5) Snapshot (§6.2) + occupancy in the same transaction.
        deadlines = compute_claim_deadlines(task, claimed_at)
        claim = AssignmentClaim(
            assignment_id=candidate.id,
            task_id=task.id,
            user_id=user_id,
            status=ClaimStatus.CLAIMED,
            claimed_at=claimed_at,
            deadline_at=deadlines.deadline_at,
            grace_deadline_at=deadlines.grace_deadline_at,
            reward_policy_snapshot=dict(REWARD_POLICY_SNAPSHOT_V1),
            base_reward_points_snapshot=task.base_reward_points,
            submission_schema_version=task.submission_schema_version,
            reward_lock_status=RewardLockStatus.NONE,
        )
        candidate.availability_status = AssignmentAvailability.OCCUPIED
        db.add(claim)
        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            mapped = _map_claim_integrity_error(exc, task_id)
            if mapped is None:
                raise
            raise mapped from exc
        # (6) Notification intent joins THIS transaction (interfaces.md
        # outbox rule): the deadline reminder rows the notifications
        # port plans here commit with the claim or not at all, and
        # dispatch only ever sees committed rows. Duck-typed through
        # NotificationEventRecorder so this module never imports
        # notifications.
        if self._notification_recorder is not None:
            await self._notification_recorder.record_event(
                db,
                event_key=f"claim:{claim.id}:created",
                event_type="CLAIM_CREATED",
                user_id=user_id,
                payload={
                    "claim_id": claim.id,
                    "deadline_at": deadlines.deadline_at,
                    "task_title": task.title,
                },
                task_policy=task,
            )
        await db.commit()
        return claim

    async def expire_claim_if_due(
        self, db: AsyncSession, claim_id: UUID, now: datetime
    ) -> ExpireResult:
        """Expire one claim when its effective deadline has passed (plan
        07 T6; spec §8.2, §11.4, §11.5/§26).

        The caller owns ``now`` (deadlines.py's aware-only philosophy):
        the expiry worker samples the SystemClock once per task attempt
        and tests freeze it. A naive instant is refused with ValueError
        BEFORE any lock or query — it has no UTC instant to compare
        against the deadline columns.

        Transaction shape (one transaction, one commit, only on the
        EXPIRED path — every other outcome writes nothing):

        1. ``SELECT ... FROM assignment_claims WHERE id = :claim_id FOR
           UPDATE`` — every decision below is judged on the locked row,
           so a concurrent finalize (a status flip or a VALIDATED
           submission) that commits before this lock lands is always
           observed, and a finalize arriving after waits and then sees
           EXPIRED.
        2. Outcome ladder, in order: MISSING (no row) -> ALREADY_TERMINAL
           (terminal statuses; idempotent replay — same terminal_at, no
           second event, no release flip) -> PROTECTED (VALIDATING /
           UNDER_REVIEW: the review pipeline owns the claim) -> NOT_DUE
           (strictly ``now < effective_expiry_deadline``; exactly-at is
           due) -> VALID_SUBMISSION (the inspector, consulted HERE under
           the lock, reports a protecting submission).
        3. Release: assignment FOR UPDATE, OCCUPIED -> AVAILABLE only —
           RETIRED/COMPLETED are sticky (spec §8.2) and never
           resurrected; the claim still terminates either way.
        4. Claim -> EXPIRED + ``terminal_at = now``, flush, ONE
           ``CLAIM_EXPIRED`` audit event through the ``event_publisher``
           seam (after the flush, before the commit — the abandon
           convention), one commit. The result is captured from the
           locked row BEFORE the commit.

        Amendment-2 strict reading (§11.5/§26, plan-04 final review):
        only a machine-VALIDATED submission protects; the inspector is
        the seam and its default answers False, so an in-window
        submission that is not yet VALIDATED does NOT block expiry. The
        merged branch wires the VALIDATED-reading inspector at the
        constructors (build_expire_service is the production site).

        No payout (spec §11.4): reward-lock fields, locked points, and
        every claim-time snapshot are untouched — expiry pays nothing,
        and reversals own the ledger.

        The lock order (claim row -> assignment row) keeps the abandon
        flow's convention of taking the assignment last; the claim
        flow's candidates use FOR UPDATE SKIP LOCKED and never wait on
        the assignment row, so no cycle can form between the services.
        """
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError(
                f"now must be a timezone-aware datetime (UTC instant), "
                f"got naive {now!r}"
            )
        claim = await db.scalar(
            select(AssignmentClaim)
            .where(AssignmentClaim.id == claim_id)
            .with_for_update()
        )
        if claim is None:
            return ExpireResult(
                claim_id=claim_id,
                outcome=ExpiryOutcome.MISSING,
                status=None,
                assignment_id=None,
                task_id=None,
                user_id=None,
                terminal_at=None,
            )
        status = ClaimStatus(claim.status)
        if status in TERMINAL_CLAIM_STATUSES:
            return _expire_result(claim, ExpiryOutcome.ALREADY_TERMINAL)
        if status not in EXPIRY_ACTIONABLE_STATUSES:
            return _expire_result(claim, ExpiryOutcome.PROTECTED)
        if now < effective_expiry_deadline(claim):
            return _expire_result(claim, ExpiryOutcome.NOT_DUE)
        if await self._valid_submission_inspector.has_valid_submission(db, claim.id):
            return _expire_result(claim, ExpiryOutcome.VALID_SUBMISSION)

        # Release the assignment under FOR UPDATE. Only OCCUPIED flips
        # back to AVAILABLE; RETIRED/COMPLETED are sticky (spec §8.2) and
        # are never resurrected. The FK guarantees the row exists; if it
        # somehow did not, the claim still terminates — the quota and the
        # reassignment exclusion must not hinge on the release succeeding.
        assignment = await db.scalar(
            select(Assignment)
            .where(Assignment.id == claim.assignment_id)
            .with_for_update()
        )
        if (
            assignment is not None
            and AssignmentAvailability(assignment.availability_status)
            is AssignmentAvailability.OCCUPIED
        ):
            assignment.availability_status = AssignmentAvailability.AVAILABLE

        claim.status = ClaimStatus.EXPIRED
        claim.terminal_at = now
        await db.flush()
        result = _expire_result(claim, ExpiryOutcome.EXPIRED)
        if self._event_publisher is not None:
            self._event_publisher.publish(
                DomainEvent(
                    event_type=CLAIM_EXPIRED,
                    aggregate_type="AssignmentClaim",
                    aggregate_id=claim.id,
                    occurred_at=now,
                    payload={
                        "user_id": str(claim.user_id),
                        "assignment_id": str(claim.assignment_id),
                        "task_id": str(claim.task_id),
                        "terminal_at": now.isoformat(),
                    },
                )
            )
        await db.commit()
        return result
