# backend/app/modules/submissions/reward_lock_service.py
"""Reward lock + claim transition after machine validation passes
(spec §11.2, §11.5, §26, §8.1; interfaces.md RewardLockStatus frozen;
plan 04 task 8).

``RewardLockService.on_validation_passed(db, submission_id)`` is the ONE
owner of the post-validation claim state (spec §8.1: 状态转换必须由服务
层集中定义): it establishes the reward lock from the FIRST
machine-passed submission and moves the claim into the review pipeline.

Transaction shape — ONE short transaction, claim row first:

1. Plain (lock-free) read of the Submission: its ``claim_id`` is the
   anchor. The FK guarantees the claim exists.
2. ``SELECT ... FROM assignment_claims WHERE id = :claim_id FOR UPDATE``
   — the claim row lock, the serialization point this service shares
   with the expiry worker (§26), the abandon service, and the claim
   flow. It is the ONLY lock taken: the user row is deliberately NOT
   locked (unlike claim/abandon) because this service has no quota
   interaction — spec §8.2 counts CLAIMED/REVISION_REQUIRED toward the
   3-actionable-claim cap, and this service only moves a claim OUT of
   those statuses, so it can only free slots, never consume one. The
   assignment row is equally untouched: availability flips are the
   expiry/abandon/completion services' business. Lock-order note: the
   validation service's tx1 locks submission -> claim; no service locks
   claim -> submission, so no cycle can form.
3. State judged on rows locked/refreshed inside the transaction (the
   §26 事务内复检 discipline): the submission is re-read with
   ``populate_existing`` under the claim lock, so a VALIDATED commit
   that lands while we wait on the lock is seen, and the claim row is
   the locked copy itself.

Gates, in order:

- **Missing submission** -> ``SubmissionNotFoundError`` (the validation
  service's typed error, re-used).
- **Not VALIDATED** -> ``SubmissionNotValidatedError``: the caller
  invoked the service outside the state machine (a sequencing bug, not
  a user-facing conflict); nothing was written.
- **Terminal claim** (COMPLETED/ABANDONED/EXPIRED — the expiry or
  abandon side won the race) -> idempotent no-op: the claim is returned
  unchanged. A terminal claim is never resurrected and never locks
  (spec §26: 最终只允许一个合法结果).

Lock semantics (spec §11.2, mirrored onto the frozen RewardLockStatus):

- ``NONE`` -> create a PROVISIONAL lock: tier fraction from
  ``reward_fraction(submission.submitted_at, claim.deadline_at,
  claim.grace_deadline_at)``, ``locked_reward_points`` from the claim's
  ``base_reward_points_snapshot``, ``reward_locked_at =
  submission.submitted_at`` (the submit instant — never worker,
  review, or lock time), plus one append-only ``RewardLockHistory``
  row (NONE -> PROVISIONAL).
- ``PROVISIONAL`` / ``CONFIRMED`` -> the EXISTING lock is preserved
  untouched: later revisions never lower it. (Submitted_at is monotonic
  with version, so a later revision's fraction can only be <= the
  first's anyway; the rule is enforced structurally — the existing
  columns are simply not written.)
- ``INVALIDATED`` -> the next valid submission establishes a NEW
  PROVISIONAL lock at ITS own submitted_at fraction, appending another
  history row (INVALIDATED -> PROVISIONAL). A revision-window
  submission whose submitted_at is at/after grace CLAMPS to the lowest
  defined tier 20% instead of rejecting (the §11.3 amendment /
  interfaces.md re-lock clamp note: the window keeps the claim alive
  past grace, so the re-lock must still reward — at the bottom tier).
  The invalidate action itself is the review flow's (task 9); the
  audit rows it wrote are never overwritten (interfaces.md:
  RewardLockHistory is immutable).

Claim transition: CLAIMED/VALIDATING/REVISION_REQUIRED -> UNDER_REVIEW
(VALIDATING is the worker-path intermediate the validation service's
tx1 sets at validation start; CLAIMED/REVISION_REQUIRED entries are the
defensive/direct-call shapes). ``latest_submission_id`` advances to the
processed submission only when its version is >= the current latest's —
a retried OLD job finishing after a newer version must not regress the
pointer.

WindowClosedError mapping, by lock path: on the FIRST lock (NONE ->
PROVISIONAL) the error is truly unreachable corruption —
``submitted_at`` was persisted at finalize inside the open window (the
upload finalize gate) and validation does not change it, so
``reward_fraction`` cannot raise there; if it somehow does, the data
is corrupted (e.g. deadlines edited behind the service layer's back)
and the call maps it to ``RewardWindowInconsistentError``
(SUBMISSION_WINDOW_CLOSED, the frozen registry member; status 500
because no client input can produce this shape) and DOES NOT
transition. On the RE-LOCK path (INVALIDATED -> PROVISIONAL) the same
error is a LEGAL shape — the §11.4 revision window keeps a
REVISION_REQUIRED claim submittable past grace, so an in-window
resubmission can carry submitted_at >= grace_deadline_at — and the
call clamps to the lowest defined tier 20% (spec §11.3 amendment /
interfaces.md re-lock clamp note) instead of failing.

Idempotency (spec §32): replaying a processed submission returns the
current claim state — no duplicate history row, no duplicate event, no
rewrite of the lock. Under concurrency the claim-row lock turns the
second caller into exactly this replay.

Events: exactly one ``REWARD_LOCKED`` audit-stream event per ESTABLISHED
lock (both NONE->PROVISIONAL and INVALIDATED->PROVISIONAL), published
after the flush and before the commit through the identity events port
(the interim in-memory/logging adapter; the audit/outbox module's
AuditService persists it later). Replays, preserved-lock revisions, and
terminal no-ops emit nothing. Like CLAIM_ABANDONED it is an audit
identifier, deliberately NOT a §25 notification event; the claim status
change itself is auditable through the claim row plus the append-only
lock history, so no second event is minted for it.

The service never reads the environment: clock and event publisher
arrive as constructor dependencies the composition root wires
(backend-engineering §11/§17).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.events import DomainEvent, DomainEventPublisher
from app.modules.submissions.cleanup_claim import ensure_no_active_cleanup_claim
from app.modules.submissions.enums import RewardLockStatus, ValidationStatus
from app.modules.submissions.models import RewardLockHistory, Submission
from app.modules.submissions.validation_service import SubmissionNotFoundError
from app.modules.tasks.deadlines import (
    FRACTION_EARLY,
    FRACTION_FULL,
    FRACTION_LATE,
    FRACTION_MID,
    WindowClosedError,
    reward_fraction,
    reward_points,
)
from app.modules.tasks.enums import ClaimStatus
from app.modules.tasks.models import AssignmentClaim

__all__ = [
    "REWARD_LOCKED",
    "RewardLockService",
    "RewardWindowInconsistentError",
    "SubmissionNotValidatedError",
]

# Audit-stream identifier for the established-lock occurrence (see
# module docstring: audit contract, not the §25 notification list).
REWARD_LOCKED = "REWARD_LOCKED"

# Claim statuses another owner already terminated; this service no-ops
# on them (spec §26: the expiry/abandon side won the race).
_TERMINAL_CLAIM_STATUSES: tuple[ClaimStatus, ...] = (
    ClaimStatus.COMPLETED,
    ClaimStatus.ABANDONED,
    ClaimStatus.EXPIRED,
)

# The statuses that still await the review pipeline; each moves to
# UNDER_REVIEW here. UNDER_REVIEW itself is the idempotent replay shape.
_REVIEW_ENTRY_STATUSES: tuple[ClaimStatus, ...] = (
    ClaimStatus.CLAIMED,
    ClaimStatus.VALIDATING,
    ClaimStatus.REVISION_REQUIRED,
)

# Spec §9.3 tier ladder as persisted percentage integers: the projection
# column ``reward_tier_locked`` and the history rows carry 100/80/50/20,
# while ``deadlines.reward_fraction`` answers the exact Decimal fraction.
_TIER_PERCENT_BY_FRACTION: dict[Decimal, int] = {
    FRACTION_FULL: 100,
    FRACTION_EARLY: 80,
    FRACTION_MID: 50,
    FRACTION_LATE: 20,
}

_FIRST_LOCK_REASON = "首次机器校验通过，锁定奖励档位。"
_RELOCK_REASON = "前一奖励锁被判无效，新有效提交按新的提交时间重新锁档。"
_RELOCK_CLAMPED_REASON = (
    "前一奖励锁被判无效，修订窗口内晚于宽限期的提交重锁，取阶梯最低档 20%。"
)
_WINDOW_INCONSISTENT_MESSAGE = "机器校验通过的提交落在提交窗口之外（数据异常）"
_NOT_VALIDATED_MESSAGE = "提交尚未通过机器校验"


class RewardWindowInconsistentError(BusinessError):
    """A FIRST lock (NONE -> PROVISIONAL) whose submission's submitted_at
    is at/after grace — truly unreachable data corruption (finalize and
    validation both passed a window that is now closed; the §11.4
    revision-window path clamps instead, see the module docstring). The
    claim is left untouched; the code stays the frozen
    SUBMISSION_WINDOW_CLOSED registry member, the transport status
    escalates to 500 because no client input can reach this shape."""

    def __init__(
        self,
        submission_id: UUID,
        submitted_at: datetime,
        grace_deadline_at: datetime,
    ) -> None:
        super().__init__(
            ErrorCode.SUBMISSION_WINDOW_CLOSED,
            _WINDOW_INCONSISTENT_MESSAGE,
            status_code=500,
            details={
                "submission_id": str(submission_id),
                "submitted_at": submitted_at.isoformat(),
                "grace_deadline_at": grace_deadline_at.isoformat(),
            },
        )


class SubmissionNotValidatedError(BusinessError):
    """on_validation_passed invoked on a submission outside the
    VALIDATED terminal state — a caller sequencing bug, not a
    user-facing conflict; nothing was written."""

    def __init__(self, submission_id: UUID, validation_status: str) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _NOT_VALIDATED_MESSAGE,
            status_code=409,
            details={
                "submission_id": str(submission_id),
                "validation_status": validation_status,
            },
        )


class RewardLockService:
    """Owns the reward-lock state machine and the UNDER_REVIEW entry."""

    def __init__(self, *, clock: Clock, events: DomainEventPublisher) -> None:
        self._clock = clock
        self._events = events

    async def on_validation_passed(
        self, db: AsyncSession, submission_id: UUID
    ) -> AssignmentClaim:
        """Lock the reward tier from the submit instant and move the
        claim into review; idempotent on replay, no-op on terminal
        claims. Commits exactly once, only on paths that wrote (the
        no-op paths still commit the read-only transaction to release
        the row lock).
        """
        # (1) Lock-free anchor read: the submission's claim_id. The FK
        # guarantees the claim row exists.
        anchor = await db.get(Submission, submission_id)
        if anchor is None:
            raise SubmissionNotFoundError(submission_id)

        # (2) The claim row lock — the serialization point (§26).
        claim = await db.scalar(
            select(AssignmentClaim)
            .where(AssignmentClaim.id == anchor.claim_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assert claim is not None  # submissions.claim_id FK

        # (3) State re-checked inside the transaction: a VALIDATED
        # commit that landed while we waited on the claim lock is seen.
        submission = await db.scalar(
            select(Submission)
            .where(Submission.id == submission_id)
            .execution_options(populate_existing=True)
        )
        assert submission is not None
        if submission.validation_status != ValidationStatus.VALIDATED.value:
            # Rollback expires every ORM instance in the session, so the
            # message values are captured BEFORE it (an expired
            # attribute access would lazy-refresh synchronously).
            observed_status = submission.validation_status
            await db.rollback()  # release the claim lock, write nothing
            raise SubmissionNotValidatedError(submission_id, observed_status)

        status = ClaimStatus(claim.status)
        if status in _TERMINAL_CLAIM_STATUSES:
            # The expiry/abandon side won the race (§26): idempotent
            # no-op — never resurrect, never lock, never event.
            await db.commit()
            return claim

        # CLOCK SAMPLING CONTRACT (abandon_service precedent): ``now``
        # is sampled after every lock and before its first consumer
        # (the event's occurred_at); the LOCKED instants themselves are
        # the submission's submitted_at, never this clock.
        now = self._clock.now()

        # (4) The lock semantics (spec §11.2).
        lock_status = RewardLockStatus(claim.reward_lock_status)
        established: tuple[str, int, int] | None = None
        if lock_status in (RewardLockStatus.NONE, RewardLockStatus.INVALIDATED):
            clamped = False
            try:
                fraction = reward_fraction(
                    submission.submitted_at,
                    claim.deadline_at,
                    claim.grace_deadline_at,
                )
            except WindowClosedError as exc:
                if lock_status is not RewardLockStatus.INVALIDATED:
                    # FIRST lock past grace: truly unreachable corruption
                    # (see module docstring). DO NOT transition; release
                    # the lock with nothing written. The instants are
                    # captured before the rollback for the same expiry
                    # reason as above.
                    observed_submitted_at = submission.submitted_at
                    observed_grace_at = claim.grace_deadline_at
                    await db.rollback()
                    raise RewardWindowInconsistentError(
                        submission_id, observed_submitted_at, observed_grace_at
                    ) from exc
                # RE-LOCK past grace: a LEGAL §11.4 revision-window shape —
                # clamp to the lowest defined tier instead of rejecting
                # (spec §11.3 amendment / interfaces.md re-lock clamp
                # note). The claim lock is held; the clamp continues the
                # transaction.
                fraction = FRACTION_LATE
                clamped = True
            tier = _TIER_PERCENT_BY_FRACTION.get(fraction)
            if tier is None:
                # deadlines.py grew a tier without updating the ladder.
                raise RuntimeError(
                    f"unknown reward fraction {fraction!r}; "
                    "_TIER_PERCENT_BY_FRACTION is out of sync"
                )
            points = reward_points(claim.base_reward_points_snapshot, fraction)
            from_status = lock_status.value
            claim.reward_lock_status = RewardLockStatus.PROVISIONAL.value
            claim.reward_tier_locked = tier
            claim.locked_reward_points = points
            claim.reward_locked_at = submission.submitted_at
            db.add(
                RewardLockHistory(
                    claim_id=claim.id,
                    submission_id=submission.id,
                    lock_status_from=from_status,
                    lock_status_to=RewardLockStatus.PROVISIONAL.value,
                    reward_tier_locked=tier,
                    locked_reward_points=points,
                    reason=(
                        _RELOCK_CLAMPED_REASON
                        if clamped
                        else (
                            _FIRST_LOCK_REASON
                            if lock_status is RewardLockStatus.NONE
                            else _RELOCK_REASON
                        )
                    ),
                )
            )
            established = (from_status, tier, points)

        # (5) Claim transition + latest pointer. The UNDER_REVIEW entry
        # carries the deletion-claim guard (hardening pass 4b, lease-aware
        # since pass 5a, safety-first since the final-pass claim-ownership
        # ruling, spec §27): entering the review pipeline protects
        # EVERY submission under the claim, so an UNFINISHED cleanup
        # claim (an overdue old version being deleted — or whose
        # worker's lease expired without the deletion settling) refuses
        # the transition with a typed 409: expiry does not prove the old
        # worker died, so it authorizes the cleanup takeover only;
        # deletion recovery is retryable (the validation job's bounded
        # autoretry covers the window, and after ``max_retries`` it
        # fails loudly for the operator). The claim transaction locks
        # the submission -> claim row pair; this service holds the
        # claim row lock here, so the two sides serialize on it (pass
        # 5a's unified serialization boundary).
        if status in _REVIEW_ENTRY_STATUSES:
            await ensure_no_active_cleanup_claim(db, claim.id)
            claim.status = ClaimStatus.UNDER_REVIEW.value
        await self._bump_latest(db, claim, submission)

        # (6) Publish after flush, before commit (abandon precedent);
        # replays and preserved-lock revisions emit nothing.
        if established is not None:
            await db.flush()
            from_status, tier, points = established
            self._events.publish(
                DomainEvent(
                    event_type=REWARD_LOCKED,
                    aggregate_type="AssignmentClaim",
                    aggregate_id=claim.id,
                    occurred_at=now,
                    payload={
                        "user_id": str(claim.user_id),
                        "task_id": str(claim.task_id),
                        "submission_id": str(submission.id),
                        "lock_status_from": from_status,
                        "lock_status_to": RewardLockStatus.PROVISIONAL.value,
                        "reward_tier_locked": tier,
                        "locked_reward_points": points,
                        "reward_locked_at": submission.submitted_at.isoformat(),
                    },
                )
            )
        await db.commit()
        return claim

    @staticmethod
    async def _bump_latest(
        db: AsyncSession, claim: AssignmentClaim, submission: Submission
    ) -> None:
        """Point ``latest_submission_id`` at the processed submission
        unless the claim already tracks a NEWER version — a retried old
        job finishing after a newer submission must not regress it."""
        if claim.latest_submission_id == submission.id:
            return
        if claim.latest_submission_id is not None:
            current_version = await db.scalar(
                select(Submission.version).where(
                    Submission.id == claim.latest_submission_id
                )
            )
            if current_version is not None and current_version >= submission.version:
                return
        claim.latest_submission_id = submission.id
