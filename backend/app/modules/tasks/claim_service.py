# backend/app/modules/tasks/claim_service.py
"""Concurrency-safe random assignment claiming (spec §8.2-8.4, §6.2, §9;
backend-engineering §5-§7; plan 03 task 6).

Transaction shape (spec §8.3, one transaction, one commit at the end):

1. ``SELECT status FROM users WHERE id = :user_id FOR UPDATE`` — a stable
   user-level resource is locked FIRST so concurrent claims by the same
   user serialize before any counting (spec §8.3: COUNT-then-INSERT alone
   is unsafe). Chosen over a ClaimQuota row / advisory lock because it
   needs no new table, the row always exists, and it doubles as the
   account-status gate.
2. Task gate: the Task row is read ``FOR SHARE`` — concurrent claims
   share the lock freely (claims stay parallel), while a lifecycle
   ``FOR UPDATE`` (pause/close) excludes them, so a PAUSED commit
   linearizes against in-flight claims instead of racing them.
3. Candidate selection: ``... WHERE availability_status = AVAILABLE AND
   id NOT IN (this user's ABANDONED/EXPIRED assignments) ORDER BY
   random() LIMIT 1 FOR UPDATE SKIP LOCKED`` — two transactions can never
   take the same assignment, and a row someone else holds is skipped, not
   waited on.
4. Claim insert + Assignment -> OCCUPIED + snapshot all in the same
   transaction; exactly one ``commit``.

Design decisions:

- **Cross-module boundary:** interfaces.md forbids importing identity ORM
  models from this module; the lock therefore goes through a typed
  Core-level ``users`` light table reading only ``status`` (whose value
  vocabulary is the frozen ``UserStatus`` enum — the same import seam
  collaborator_service uses for ``Role``). The port has no locking read;
  if interfaces.md registers one, this query moves behind it.
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
  (provisional pending the T9 transport review, like deadlines.py's 400);
  ACCOUNT_NOT_ACTIVE stays 403 per the identity precedent; a missing
  user or task is 404 NOT_FOUND like TaskNotFoundError.
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
  stays claimable on both paths. Task-level eligibility refinements
  (per-task restrictions, role gating, full cutoff semantics) arrive in
  plan 03 task 7; this service implements the checklist's core window
  checks.

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
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import String, Uuid, column, func, select, table
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.enums import UserStatus
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
    "ActiveClaimExistsError",
    "AssignmentLimitReachedError",
    "ClaimCutoffReachedError",
    "ClaimService",
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
# Core-level light table, NOT the identity ORM model.
_USERS_LOCK = table(
    "users",
    column("id", Uuid),
    column("status", String),
)

# Partial unique indexes on assignment_claims (models.py) — the integrity
# backstop translated below when a race slips past SKIP LOCKED.
_UQ_ACTIVE_ASSIGNMENT = "uq_assignment_claims_active_assignment"
_UQ_ACTIVE_USER_TASK = "uq_assignment_claims_active_user_task"
_CONSTRAINT_IN_MESSAGE = re.compile(r'constraint "(?P<name>[^"]+)"')


# --- messages (§29 envelope text) --------------------------------------------------


_USER_NOT_FOUND_MESSAGE = "用户不存在"
_ACCOUNT_NOT_ACTIVE_MESSAGE = "账号当前状态不允许执行该操作"
_TASK_NOT_CLAIMABLE_MESSAGE = "任务当前不可领取"
_CUTOFF_REACHED_MESSAGE = "距任务截止时间已不足，已停止新领取"
_LIMIT_REACHED_MESSAGE = "当前进行中的任务已达到上限"
_ACTIVE_CLAIM_EXISTS_MESSAGE = "该任务已有进行中的领取，不能重复领取"
_NO_ASSIGNMENT_MESSAGE = "该任务已无可领取的数据单元"


# --- typed exceptions (router-mapped; T9 seam) --------------------------------------


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


# --- the service ---------------------------------------------------------------------


class ClaimService:
    """Random assignment claiming under contention (spec §8.2-8.3).

    ``max_active_claims`` is injectable for tests; production wires the
    spec §8.2 default of 3.
    """

    def __init__(
        self, *, clock: Clock, max_active_claims: int = MAX_ACTIVE_CLAIMS
    ) -> None:
        self._clock = clock
        self._max_active_claims = max_active_claims

    async def claim_random_assignment(
        self, db: AsyncSession, user_id: UUID, task_id: UUID
    ) -> AssignmentClaim:
        """Claim one random AVAILABLE assignment of the task for the user.

        Raises the §8.4 business codes (4xx) for every checklist failure;
        commits exactly once, only on the success path.
        """
        claimed_at = self._clock.now()

        # (1) Same-user serialization: lock the stable user-level resource
        # FIRST (spec §8.3), then gate on the row we actually locked.
        status_value = (
            await db.execute(
                select(_USERS_LOCK.c.status)
                .where(_USERS_LOCK.c.id == user_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if status_value is None:
            raise UserNotFoundError(user_id)
        if UserStatus(status_value) is not UserStatus.ACTIVE:
            raise AccountNotActiveError(user_id)

        # (2) Task gate under FOR SHARE (see module docstring).
        task = await db.scalar(
            select(Task).where(Task.id == task_id).with_for_update(read=True)
        )
        if task is None:
            raise TaskNotFoundError(task_id)

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
            self._require_within_cutoff(task, claimed_at)

        # (3) Global per-student quota — safe only because every claim by
        # this user holds the user-row lock (spec §8.3).
        occupying = (
            await db.execute(
                select(func.count())
                .select_from(AssignmentClaim)
                .where(
                    AssignmentClaim.user_id == user_id,
                    AssignmentClaim.status.in_(QUOTA_OCCUPYING_STATUSES),
                )
            )
        ).scalar_one()
        if occupying >= self._max_active_claims:
            raise AssignmentLimitReachedError(self._max_active_claims)

        # (4) One non-terminal claim per user per task (§8.2); the
        # partial unique index backstops this friendly check.
        conflicting = await db.scalar(
            select(AssignmentClaim.id)
            .where(
                AssignmentClaim.user_id == user_id,
                AssignmentClaim.task_id == task_id,
                AssignmentClaim.status.in_(ACTIVE_CLAIM_STATUSES),
            )
            .limit(1)
        )
        if conflicting is not None:
            raise ActiveClaimExistsError(task_id)

        # (5) Random candidate under FOR UPDATE SKIP LOCKED, excluding
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

        # (6) Snapshot (§6.2) + occupancy in the same transaction.
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

    @staticmethod
    def _require_within_cutoff(task: Task, now: datetime) -> None:
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
