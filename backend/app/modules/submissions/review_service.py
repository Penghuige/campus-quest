# backend/app/modules/submissions/review_service.py
"""Human review: revision window, reward-lock invalidation, approve
(spec §11.3, §11.4, §14, §30; interfaces.md ReviewStatus / ReviewAction
/ RewardLockStatus / PointsRewardPort frozen; plan 04 task 9).

Three use cases, one shared discipline:

- ``ReviewService.require_revision(db, actor, submission_id, note)`` —
  the ordinary quality loop (§11.3's REVISION_REQUIRED): the claim
  moves to REVISION_REQUIRED with ``revision_deadline_at`` per §11.4
  (``max(existing revision_deadline_at-or-grace, reviewed_at + 24h)``;
  a re-退回 recomputes from the NEW reviewed_at, so the deadline only
  ever extends), the existing non-invalidated lock is preserved
  untouched, one append-only SubmissionReview row records the decision.
- ``ReviewService.invalidate_reward_lock(db, actor, submission_id,
  reason)`` — §11.3's INVALIDATE_REWARD_LOCK: the reviewer reason is
  mandatory, the PROVISIONAL lock is cancelled (projection columns
  cleared, the previous tier/points/reason survive in one append-only
  RewardLockHistory row plus the audit event), and the claim moves to
  REVISION_REQUIRED with a §11.4 deadline from THIS reviewed_at — the
  student must resubmit; the next valid submission re-locks through
  ``reward_lock_service`` (past-grace re-locks clamp to 20%, spec §11.3
  amendment / interfaces.md re-lock clamp note).
- ``ReviewService.approve_submission(db, actor, submission_id)`` — the
  §14 ten-step transaction below.

Transaction shape (backend-engineering §5) — ONE short transaction per
call, claim row first, mirroring ``reward_lock_service``:

1. Plain (lock-free) read of the Submission: its ``claim_id`` is the
   anchor; the FK guarantees the claim exists.
2. ``SELECT ... FROM assignment_claims WHERE id = :claim_id FOR
   UPDATE`` — the claim row lock, the serialization point shared with
   the expiry worker, abandon, and the reward-lock service. It is the
   first lock taken; approve additionally locks the assignment row
   AFTER the claim (the sanctioned claim -> assignment order — the same
   order the §26 expiry candidate uses), and no service locks claim ->
   submission, so no cycle can form.
3. State judged on rows locked/refreshed inside the transaction: the
   submission is re-read with ``populate_existing`` under the claim
   lock (the 事务内复检 discipline), which is what turns two
   concurrent approves into exactly one grant + one idempotent
   ALREADY_REVIEWED result.

Gates, in order (typed rejections; attribute values are captured
before the rollback because it expires every ORM instance):

- Missing submission -> ``SubmissionNotFoundError`` (the validation
  service's typed error, re-used).
- Claim COMPLETED (approve only) -> the idempotent path: return
  ``ApprovalResult(already_reviewed=True)`` with nothing written (§14:
  幂等成功或 ALREADY_REVIEWED); the read-only transaction still commits
  to release the row lock. ABANDONED/EXPIRED (all actions) ->
  ``ClaimNotReviewableError`` — a terminal claim is never resurrected.
- Submission not machine-VALIDATED -> ``SubmissionNotValidatedError``
  (the reward-lock service's typed error, re-used): the human stage is
  unreachable before the machine stage (§11.1).
- Stale version -> ``StaleSubmissionVersionError``: only the claim's
  ``latest_submission_id`` can be reviewed — older VALIDATED versions
  exist once the student resubmitted.
- Reviewer permission -> ``ReviewerPermissionDeniedError``: the task
  owner, an Admin, or a collaborator holding REVIEW_SUBMISSIONS (spec
  §4.2/§4.3). The check reuses the tasks-module collaborator query
  pattern — ``Task``/``TaskCollaborator`` are read through the frozen
  ``CollaboratorPermission`` vocabulary from the tasks module's
  collaborator service (no identity ORM: account facts stay behind the
  ``UserDirectory`` port per interfaces.md). It runs BEFORE the
  lock-shape gates (approve only): an unauthorized teacher must learn
  PERMISSION_DENIED, never the claim's lock state.
- Lock-shape gates: invalidate requires PROVISIONAL (CONFIRMED is
  final — the reward was granted on it; NONE has nothing to cancel);
  approve requires PROVISIONAL (step 7 confirms values that only a
  provisional lock carries).

The approve transaction, §14's ten steps verbatim: (1) lock claim;
(2) verify the claim is still reviewable; (3) verify the submission is
VALIDATED, current, and belongs to the claim; (4) verify reviewer
permission; (5) Submission -> APPROVED; (6) Claim -> COMPLETED +
``terminal_at``; (7) reward_lock -> CONFIRMED + one RewardLockHistory
row carrying the confirmed tier/points; (8) the frozen
``PointsRewardPort.grant_assignment_reward`` — called inside the
transaction with a stable ``assignment_reward:<claim_id>`` idempotency
key; the UNIQUE(claim) ledger semantics are Plan 05's concrete
adapter; (9) Assignment -> COMPLETED (permanently unallocatable);
(10) the SUBMISSION_APPROVED audit event.

AFTER the ten-step transaction commits, the approve path runs the
post-commit honor trigger (spec §18; plan 05 final review I3): the
completed user's LIFETIME honors are evaluated through the
``ClaimCompletedHonorsPort`` seam in their own transaction, wrapped so
a honor failure can never fail the already-committed approval (the
projection-dispatcher pattern; see the port's docstring for the
Plan 07/08 rank-honor producer ruling).

Events follow the outbox direction (interfaces.md Core Primitives):
publication goes through the ``DomainEventPublisher`` port after the
flush and before the commit — the interim logging adapter — and never
as a direct SMS/email/Celery side effect inside the transaction.
``REVISION_REQUIRED`` and ``SUBMISSION_APPROVED`` are frozen §25
notification event names; ``REWARD_LOCK_INVALIDATED`` is an
audit-stream identifier (the ``REWARD_LOCKED`` precedent — §30 audit
material, deliberately not on the §25 delivery list). The invalidation
payload is audit-grade: actor id, reason, previous lock values — no
secrets, no contact data.

The service never reads the environment: clock, event publisher, and
the points port arrive as constructor dependencies the composition
root wires (backend-engineering §11/§17/§13).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.rbac import is_admin
from app.modules.identity.events import Actor, DomainEvent, DomainEventPublisher
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
)
from app.modules.submissions.reward_lock_service import SubmissionNotValidatedError
from app.modules.submissions.validation_service import SubmissionNotFoundError
from app.modules.tasks.collaborator_service import CollaboratorPermission
from app.modules.tasks.enums import AssignmentAvailability, ClaimStatus
from app.modules.tasks.models import (
    Assignment,
    AssignmentClaim,
    Task,
    TaskCollaborator,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ApprovalResult",
    "ClaimCompletedHonorsPort",
    "ClaimNotReviewableError",
    "InvalidationReasonRequiredError",
    "LockNotInvalidatableError",
    "LockNotProvisionalError",
    "PointsRewardPort",
    "GrantResult",
    "REVISION_REQUIRED_EVENT",
    "ReviewerPermissionDeniedError",
    "ReviewService",
    "REWARD_LOCK_INVALIDATED_EVENT",
    "StaleSubmissionVersionError",
    "SUBMISSION_APPROVED_EVENT",
]

# Frozen §25 notification event names (interfaces.md Domain Events).
REVISION_REQUIRED_EVENT = "REVISION_REQUIRED"
SUBMISSION_APPROVED_EVENT = "SUBMISSION_APPROVED"
# Audit-stream identifier (the REWARD_LOCKED precedent in
# reward_lock_service): §30 audit material, NOT a §25 delivery event.
REWARD_LOCK_INVALIDATED_EVENT = "REWARD_LOCK_INVALIDATED"

# Spec §11.4: revision_deadline_at = max(baseline, reviewed_at + 24h).
_REVISION_WINDOW = timedelta(hours=24)

_TERMINAL_CLAIM_STATUSES: tuple[ClaimStatus, ...] = (
    ClaimStatus.COMPLETED,
    ClaimStatus.ABANDONED,
    ClaimStatus.EXPIRED,
)

_PERMISSION_DENIED_MESSAGE = "只有任务所有者、拥有审核权限的协作者或管理员可以审核提交"
_STALE_VERSION_MESSAGE = "只能审核当前最新版本的有效提交"
_CLAIM_NOT_REVIEWABLE_MESSAGE = "该认领已结束，无法再审核"
_LOCK_NOT_INVALIDATABLE_MESSAGE = "当前奖励锁状态不支持判无效操作"
_REASON_REQUIRED_MESSAGE = "判无效操作必须填写原因"
_LOCK_NOT_PROVISIONAL_MESSAGE = "奖励锁尚未处于待确认状态，无法通过验收"

_CONFIRM_LOCK_REASON = "人工验收通过，确认奖励锁。"


# --- the frozen points port (interfaces.md, cross-module ports) ----------------------


@dataclass(frozen=True, slots=True)
class GrantResult:
    """Outcome of one grant call; Plan 05's concrete adapter fills the
    ledger identity behind the same frozen shape."""

    user_id: UUID
    claim_id: UUID
    points_granted: int


@runtime_checkable
class PointsRewardPort(Protocol):
    """Points module boundary (interfaces.md): the review-approve
    transaction hands the reward grant to the points module through
    this port — Plan 04 tests against a fake, Plan 05 provides the
    concrete PointsLedger implementation.

    ``locked_points`` is the reward lock's ``locked_reward_points``
    (the fraction-adjusted amount, already floored per §31.1);
    ``idempotency_key`` is stable per claim, so a replayed or racing
    approve cannot double-grant (UNIQUE(claim) semantics live in Plan
    05's adapter).
    """

    async def grant_assignment_reward(
        self,
        *,
        user_id: UUID,
        claim_id: UUID,
        base_points: int,
        locked_points: int,
        idempotency_key: str,
    ) -> GrantResult: ...


@runtime_checkable
class ClaimCompletedHonorsPort(Protocol):
    """Rankings module boundary (spec §18; plan 05 final review I3):
    the approve path hands the completed user to honor evaluation AFTER
    the approve transaction committed — never inside it.

    The binding is FAILURE-TOLERANT BY CONTRACT in the caller: the
    approve has already committed by the time this runs, so an
    exception must be swallowed by the caller's defensive wrapper (an
    honor can never fail an approval; a lost evaluation self-heals on
    any later claim completion because the honor rules recompute facts).
    Production wires the rankings module's ``ClaimCompletedHonorsTrigger``
    (lifetime rules only — the DAILY_RANK/MONTHLY_RANK producers are
    Plan 07/08's scheduled beat, honor_service's ruling).
    """

    async def on_claim_completed(self, session: AsyncSession, user_id: UUID) -> Any: ...


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    """What ``approve_submission`` hands back.

    ``already_reviewed`` is True only on the §14 idempotent path (the
    claim was already COMPLETED under the row lock): ``grant`` is None
    and nothing was written.
    """

    claim: AssignmentClaim
    grant: GrantResult | None
    already_reviewed: bool


# --- typed exceptions (router-mapped) ------------------------------------------------


class ReviewerPermissionDeniedError(BusinessError):
    """The actor is neither the task owner, an Admin, nor a collaborator
    holding REVIEW_SUBMISSIONS (spec §4.2/§4.3)."""

    def __init__(self, task_id: UUID, actor_id: UUID) -> None:
        super().__init__(
            ErrorCode.PERMISSION_DENIED,
            _PERMISSION_DENIED_MESSAGE,
            status_code=403,
            details={"task_id": str(task_id), "actor_id": str(actor_id)},
        )


class StaleSubmissionVersionError(BusinessError):
    """The submission is not the claim's ``latest_submission_id``: the
    student resubmitted, so only the newer version may be reviewed.

    The registry has no dedicated stale-version code; this carries
    ``VALIDATION_ERROR`` with HTTP 409 and the router may remap it if
    interfaces.md registers one (doc-first rule — the
    ``DuplicateCollaboratorError`` posture)."""

    def __init__(self, submission_id: UUID, latest_submission_id: UUID | None) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _STALE_VERSION_MESSAGE,
            status_code=409,
            details={
                "submission_id": str(submission_id),
                "latest_submission_id": (
                    str(latest_submission_id)
                    if latest_submission_id is not None
                    else None
                ),
            },
        )


class ClaimNotReviewableError(BusinessError):
    """The claim is terminal (ABANDONED/EXPIRED, or COMPLETED outside
    the approve idempotent path) — no review action may resurrect it."""

    def __init__(self, claim_id: UUID, status: str) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _CLAIM_NOT_REVIEWABLE_MESSAGE,
            status_code=409,
            details={"claim_id": str(claim_id), "status": status},
        )


class LockNotInvalidatableError(BusinessError):
    """Only a PROVISIONAL lock can be invalidated: CONFIRMED already
    granted the reward on it, and NONE means nothing is locked."""

    def __init__(self, submission_id: UUID, reward_lock_status: str) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _LOCK_NOT_INVALIDATABLE_MESSAGE,
            status_code=409,
            details={
                "submission_id": str(submission_id),
                "reward_lock_status": reward_lock_status,
            },
        )


class InvalidationReasonRequiredError(BusinessError):
    """The reviewer reason is blank (spec §11.3: 必须要求 reviewer
    reason). Rejected before any row is locked."""

    def __init__(self, submission_id: UUID) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _REASON_REQUIRED_MESSAGE,
            status_code=400,
            details={"submission_id": str(submission_id)},
        )


class LockNotProvisionalError(BusinessError):
    """Approve reached a claim whose lock is not PROVISIONAL (the
    on_validation_passed race, or an invalidated lock awaiting
    resubmission): step 7 must confirm real values, never invent them."""

    def __init__(self, claim_id: UUID, reward_lock_status: str) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _LOCK_NOT_PROVISIONAL_MESSAGE,
            status_code=409,
            details={
                "claim_id": str(claim_id),
                "reward_lock_status": reward_lock_status,
            },
        )


# --- the service ---------------------------------------------------------------------


class ReviewService:
    """Owns the human review stage (spec §11.3/§11.4/§14).

    ``points`` is the frozen cross-module port (fake in this plan's
    tests, the Plan 05 points module in production); clock and event
    publisher follow the reward-lock service's injection shape.
    ``honors`` (final review I3) is the post-commit honor trigger —
    ``None`` disables the evaluation (Plan-04 shape, unchanged tests);
    production binds the rankings module's binding, and the wrapper
    below makes its failure irrelevant to the approval.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        events: DomainEventPublisher,
        points: PointsRewardPort,
        honors: ClaimCompletedHonorsPort | None = None,
    ) -> None:
        self._clock = clock
        self._events = events
        self._points = points
        self._honors = honors

    # -- require_revision (§11.3 REVISION_REQUIRED, §11.4 window) -------------------

    async def require_revision(
        self,
        db: AsyncSession,
        actor: Actor,
        submission_id: UUID,
        note: str | None,
    ) -> AssignmentClaim:
        """Return the submission for fixes; the existing lock survives.

        The note is the teacher's guidance (stored stripped; blank
        becomes NULL — only the §11.3 invalidation reason is mandatory).
        Commits exactly once.
        """
        submission, claim, now = await self._locked_reviewable(db, actor, submission_id)
        note_text = note.strip() if note is not None and note.strip() else None
        deadline = self._revision_deadline(claim, now)

        submission.review_status = ReviewStatus.REVISION_REQUIRED.value
        submission.reviewer_id = actor.user_id
        submission.reviewed_at = now
        submission.review_note = note_text
        claim.status = ClaimStatus.REVISION_REQUIRED.value
        claim.revision_deadline_at = deadline
        db.add(
            SubmissionReview(
                submission_id=submission.id,
                reviewer_id=actor.user_id,
                action=ReviewAction.REQUIRE_REVISION.value,
                note=note_text,
            )
        )
        await db.flush()
        self._events.publish(
            DomainEvent(
                event_type=REVISION_REQUIRED_EVENT,
                aggregate_type="AssignmentClaim",
                aggregate_id=claim.id,
                occurred_at=now,
                payload={
                    "user_id": str(claim.user_id),
                    "task_id": str(claim.task_id),
                    "submission_id": str(submission.id),
                    "reviewer_id": str(actor.user_id),
                    "revision_deadline_at": deadline.isoformat(),
                    "note": note_text,
                },
            )
        )
        await db.commit()
        return claim

    # -- invalidate_reward_lock (§11.3 INVALIDATE_REWARD_LOCK) ----------------------

    async def invalidate_reward_lock(
        self,
        db: AsyncSession,
        actor: Actor,
        submission_id: UUID,
        reason: str,
    ) -> AssignmentClaim:
        """Cancel the PROVISIONAL lock and demand a resubmission.

        The reason is mandatory (spec §11.3); the previous lock values
        survive in the append-only history row and the audit event
        payload (spec §11.2: INVALIDATED 审计历史不得被覆盖). Commits
        exactly once.
        """
        reason_text = reason.strip() if reason is not None else ""
        if not reason_text:
            raise InvalidationReasonRequiredError(submission_id)

        submission, claim, now = await self._locked_reviewable(db, actor, submission_id)
        if claim.reward_lock_status != RewardLockStatus.PROVISIONAL.value:
            observed_lock_status = claim.reward_lock_status
            await db.rollback()  # release the claim lock, write nothing
            raise LockNotInvalidatableError(submission_id, observed_lock_status)

        previous_tier = claim.reward_tier_locked
        previous_points = claim.locked_reward_points
        previous_locked_at = claim.reward_locked_at
        deadline = self._revision_deadline(claim, now)

        # Cancel the provisional lock: the projection clears, the
        # history row below keeps the cancelled values (§11.2/§11.3).
        claim.reward_lock_status = RewardLockStatus.INVALIDATED.value
        claim.reward_tier_locked = None
        claim.locked_reward_points = None
        claim.reward_locked_at = None
        claim.status = ClaimStatus.REVISION_REQUIRED.value
        claim.revision_deadline_at = deadline
        submission.review_status = ReviewStatus.REVISION_REQUIRED.value
        submission.reviewer_id = actor.user_id
        submission.reviewed_at = now
        submission.review_note = reason_text
        db.add(
            RewardLockHistory(
                claim_id=claim.id,
                submission_id=submission.id,
                lock_status_from=RewardLockStatus.PROVISIONAL.value,
                lock_status_to=RewardLockStatus.INVALIDATED.value,
                reward_tier_locked=previous_tier,
                locked_reward_points=previous_points,
                changed_by=actor.user_id,
                reason=reason_text,
            )
        )
        db.add(
            SubmissionReview(
                submission_id=submission.id,
                reviewer_id=actor.user_id,
                action=ReviewAction.INVALIDATE_LOCK.value,
                note=reason_text,
            )
        )
        await db.flush()
        self._events.publish(
            DomainEvent(
                event_type=REWARD_LOCK_INVALIDATED_EVENT,
                aggregate_type="AssignmentClaim",
                aggregate_id=claim.id,
                occurred_at=now,
                payload={
                    "user_id": str(claim.user_id),
                    "task_id": str(claim.task_id),
                    "submission_id": str(submission.id),
                    "reviewer_id": str(actor.user_id),
                    "reason": reason_text,
                    "lock_status_from": RewardLockStatus.PROVISIONAL.value,
                    "lock_status_to": RewardLockStatus.INVALIDATED.value,
                    "previous_reward_tier_locked": previous_tier,
                    "previous_locked_reward_points": previous_points,
                    "previous_reward_locked_at": (
                        previous_locked_at.isoformat()
                        if previous_locked_at is not None
                        else None
                    ),
                },
            )
        )
        await db.commit()
        return claim

    # -- approve_submission (§14, the ten-step transaction) -------------------------

    async def approve_submission(
        self, db: AsyncSession, actor: Actor, submission_id: UUID
    ) -> ApprovalResult:
        """Approve the submission: claim COMPLETED, lock CONFIRMED,
        reward granted through the points port, assignment COMPLETED —
        all in ONE transaction. A claim that is already COMPLETED under
        the row lock returns the idempotent ALREADY_REVIEWED result
        (nothing written, no second grant)."""
        # Step 1: the claim row lock — the serialization point.
        anchor = await db.get(Submission, submission_id)
        if anchor is None:
            raise SubmissionNotFoundError(submission_id)
        claim = await db.scalar(
            select(AssignmentClaim)
            .where(AssignmentClaim.id == anchor.claim_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assert claim is not None  # submissions.claim_id FK

        # Step 2: verify the claim is still reviewable (in-transaction
        # recheck — this is the gate the racing loser fails).
        status = ClaimStatus(claim.status)
        if status is ClaimStatus.COMPLETED:
            await db.commit()  # release the lock, write nothing
            return ApprovalResult(claim=claim, grant=None, already_reviewed=True)
        if status in _TERMINAL_CLAIM_STATUSES:
            observed_status = claim.status
            observed_claim_id = claim.id
            await db.rollback()
            raise ClaimNotReviewableError(observed_claim_id, observed_status)

        # Step 3: the submission belongs (by anchor construction), is
        # machine-VALIDATED, and is the current version.
        submission = await db.scalar(
            select(Submission)
            .where(Submission.id == submission_id)
            .execution_options(populate_existing=True)
        )
        assert submission is not None
        await self._require_submission_gates(db, claim, submission)

        # Step 4: reviewer permission (owner / REVIEW_SUBMISSIONS
        # collaborator / Admin) — deliberately BEFORE the lock-shape
        # gate below (the invalidate path's permission-first ordering):
        # an unauthorized teacher must learn PERMISSION_DENIED, never
        # the claim's lock state (a lock-state oracle for free).
        await self._require_review_permission(db, claim, actor)

        # Step 7 needs real values to confirm: only a PROVISIONAL lock
        # carries them (the on_validation_passed race, or a lock
        # invalidated pending resubmission, is a typed rejection).
        if claim.reward_lock_status != RewardLockStatus.PROVISIONAL.value:
            observed_lock_status = claim.reward_lock_status
            observed_claim_id = claim.id
            await db.rollback()
            raise LockNotProvisionalError(observed_claim_id, observed_lock_status)

        # CLOCK SAMPLING CONTRACT (reward_lock_service precedent):
        # ``now`` is sampled after every lock and before its first
        # consumer (reviewed_at / terminal_at / the event's occurred_at).
        now = self._clock.now()

        # Step 5: Submission -> APPROVED.
        submission.review_status = ReviewStatus.APPROVED.value
        submission.reviewer_id = actor.user_id
        submission.reviewed_at = now
        submission.review_note = None

        # Step 6: Claim -> COMPLETED (terminal).
        claim.status = ClaimStatus.COMPLETED.value
        claim.terminal_at = now

        # Step 7: reward_lock -> CONFIRMED at the locked values.
        tier = claim.reward_tier_locked
        points_value = claim.locked_reward_points
        assert tier is not None and points_value is not None  # PROVISIONAL gate
        claim.reward_lock_status = RewardLockStatus.CONFIRMED.value
        db.add(
            RewardLockHistory(
                claim_id=claim.id,
                submission_id=submission.id,
                lock_status_from=RewardLockStatus.PROVISIONAL.value,
                lock_status_to=RewardLockStatus.CONFIRMED.value,
                reward_tier_locked=tier,
                locked_reward_points=points_value,
                changed_by=actor.user_id,
                reason=_CONFIRM_LOCK_REASON,
            )
        )
        db.add(
            SubmissionReview(
                submission_id=submission.id,
                reviewer_id=actor.user_id,
                action=ReviewAction.APPROVE.value,
                note=None,
            )
        )

        # Step 9: Assignment -> COMPLETED — permanently unallocatable
        # (§8.2: COMPLETED never returns to AVAILABLE). Locked AFTER the
        # claim, the sanctioned claim -> assignment order. Only OCCUPIED
        # flips (the abandon_service guard): RETIRED/COMPLETED are
        # sticky terminal assignment states, so a unit that already left
        # OCCUPIED is a no-op here with the same terminal outcome — the
        # claim still completes, and a pre-terminal assignment must
        # never error or resurrect the approve.
        assignment = await db.scalar(
            select(Assignment)
            .where(Assignment.id == claim.assignment_id)
            .with_for_update()
        )
        assert assignment is not None  # claims.assignment_id FK
        if (
            AssignmentAvailability(assignment.availability_status)
            is AssignmentAvailability.OCCUPIED
        ):
            assignment.availability_status = AssignmentAvailability.COMPLETED.value

        await db.flush()

        # Step 8: the unique reward grant, through the frozen port,
        # inside the transaction (§14: one transaction covers the whole
        # invariant; Plan 05's adapter writes the ledger on this session).
        grant = await self._points.grant_assignment_reward(
            user_id=claim.user_id,
            claim_id=claim.id,
            base_points=claim.base_reward_points_snapshot,
            locked_points=points_value,
            idempotency_key=f"assignment_reward:{claim.id}",
        )

        # Step 10: the audit event, after flush / before commit.
        payload: dict[str, Any] = {
            "user_id": str(claim.user_id),
            "task_id": str(claim.task_id),
            "submission_id": str(submission.id),
            "reviewer_id": str(actor.user_id),
            "reward_tier_locked": tier,
            "locked_reward_points": points_value,
            "terminal_at": now.isoformat(),
        }
        self._events.publish(
            DomainEvent(
                event_type=SUBMISSION_APPROVED_EVENT,
                aggregate_type="AssignmentClaim",
                aggregate_id=claim.id,
                occurred_at=now,
                payload=payload,
            )
        )
        # Captured before the commit: the honor trigger runs on the
        # other side of it, and instance expiry must not matter.
        student_id = claim.user_id
        await db.commit()
        # Post-commit honor trigger (final review I3): the approval is
        # already durable; the evaluation is a separate, best-effort
        # transaction that must never fail it. Only the path that
        # GRANTED evaluates — the idempotent replay returned earlier
        # and changed no honor fact.
        if self._honors is not None:
            await self._evaluate_honors_defensively(db, student_id)
        return ApprovalResult(claim=claim, grant=grant, already_reviewed=False)

    async def _evaluate_honors_defensively(
        self, db: AsyncSession, user_id: UUID
    ) -> None:
        """Run the honor evaluation in its OWN transaction and swallow
        any failure (the projection-dispatcher pattern: a post-commit
        side effect is repaired by later triggers, never by failing the
        business write it follows — a lost evaluation self-heals on any
        later claim completion because the honor rules recompute facts).
        The rollback keeps the session usable for the caller's teardown;
        the approve that reached here is already durable."""
        honors = self._honors
        if honors is None:
            return
        try:
            await honors.on_claim_completed(db, user_id)
            await db.commit()
        except Exception:
            await db.rollback()
            logger.warning(
                "review_approve.honor_evaluation_failed",
                extra={"user_id": str(user_id)},
                exc_info=True,
            )

    # -- shared preamble --------------------------------------------------------------

    async def _locked_reviewable(
        self, db: AsyncSession, actor: Actor, submission_id: UUID
    ) -> tuple[Submission, AssignmentClaim, datetime]:
        """Lock the claim, recheck the state inside the transaction, run
        the submission gates and the permission gate; return the pair
        plus the sampled ``now`` (after every lock, before every
        consumer)."""
        anchor = await db.get(Submission, submission_id)
        if anchor is None:
            raise SubmissionNotFoundError(submission_id)
        claim = await db.scalar(
            select(AssignmentClaim)
            .where(AssignmentClaim.id == anchor.claim_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assert claim is not None  # submissions.claim_id FK

        status = ClaimStatus(claim.status)
        if status in _TERMINAL_CLAIM_STATUSES:
            observed_status = claim.status
            observed_claim_id = claim.id
            await db.rollback()  # release the claim lock, write nothing
            raise ClaimNotReviewableError(observed_claim_id, observed_status)

        submission = await db.scalar(
            select(Submission)
            .where(Submission.id == submission_id)
            .execution_options(populate_existing=True)
        )
        assert submission is not None
        await self._require_submission_gates(db, claim, submission)
        await self._require_review_permission(db, claim, actor)
        return submission, claim, self._clock.now()

    async def _require_submission_gates(
        self,
        db: AsyncSession,
        claim: AssignmentClaim,
        submission: Submission,
    ) -> None:
        """The §14 step-3 submission checks: machine-VALIDATED and the
        claim's current version (stale versions are a typed rejection —
        the student resubmitted, so the older one is decided history).
        Every attribute read by the error is captured before the
        rollback, which expires the ORM instances."""
        submission_id = submission.id
        if submission.validation_status != ValidationStatus.VALIDATED.value:
            observed_status = submission.validation_status
            await db.rollback()  # values captured above: rollback expires
            raise SubmissionNotValidatedError(submission_id, observed_status)
        if claim.latest_submission_id != submission.id:
            observed_latest = claim.latest_submission_id
            await db.rollback()
            raise StaleSubmissionVersionError(submission_id, observed_latest)

    async def _require_review_permission(
        self, db: AsyncSession, claim: AssignmentClaim, actor: Actor
    ) -> None:
        """Owner, Admin, or a REVIEW_SUBMISSIONS collaborator (spec
        §4.2/§4.3) — the tasks-module collaborator query pattern
        (``_require_statistics_access``) with the frozen
        ``CollaboratorPermission`` vocabulary. Lock-free: capability
        rows are grant-time state, and the caller already holds the
        claim row lock that serializes the review decision itself."""
        task_id = claim.task_id
        owner_id = await db.scalar(
            select(Task.owner_teacher_id).where(Task.id == task_id)
        )
        if owner_id is None or owner_id == actor.user_id or is_admin(actor.role):
            # The FK guarantees the task exists; owner/Admin hold every
            # capability (the add_collaborator owner exemption, §4.3).
            return
        permissions = await db.scalar(
            select(TaskCollaborator.permissions).where(
                TaskCollaborator.task_id == task_id,
                TaskCollaborator.teacher_id == actor.user_id,
            )
        )
        if (
            permissions is None
            or CollaboratorPermission.REVIEW_SUBMISSIONS not in permissions
        ):
            await db.rollback()
            raise ReviewerPermissionDeniedError(task_id, actor.user_id)

    @staticmethod
    def _revision_deadline(claim: AssignmentClaim, now: datetime) -> datetime:
        """§11.4: ``max(existing revision_deadline_at-or-grace,
        reviewed_at + 24h)`` — reviewing late must not cost the student
        the revision window, and a re-退回 recomputes from the NEW
        reviewed_at, so the deadline extends monotonically."""
        baseline = (
            claim.revision_deadline_at
            if claim.revision_deadline_at is not None
            else claim.grace_deadline_at
        )
        return max(baseline, now + _REVISION_WINDOW)
