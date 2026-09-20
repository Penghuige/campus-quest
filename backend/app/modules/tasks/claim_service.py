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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import String, Uuid, column, func, select, table
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.enums import Role, UserStatus
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
    "MAX_ACTIVE_CLAIMS",
    "QUOTA_OCCUPYING_STATUSES",
    "REASSIGN_EXCLUDED_STATUSES",
    "REWARD_POLICY_SNAPSHOT_V1",
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
    "NoAssignmentAvailableError",
    "TaskNotClaimableError",
    "UserNotFoundError",
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


# --- the service ---------------------------------------------------------------------


class ClaimService:
    """Random assignment claiming under contention (spec §8.2-8.3).

    ``max_active_claims`` is injectable for tests (forwarded to the
    eligibility rules); production wires the spec §8.2 default of 3.
    """

    def __init__(
        self, *, clock: Clock, max_active_claims: int = MAX_ACTIVE_CLAIMS
    ) -> None:
        self._clock = clock
        self._eligibility = ClaimEligibilityService(max_active_claims=max_active_claims)

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
        await db.commit()
        return claim
