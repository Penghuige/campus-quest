# backend/app/modules/tasks/abandon_service.py
"""Student-initiated claim abandon and assignment release (spec §8.5,
§8.2, §0, §44.14; backend-engineering §11).

Transaction shape (one transaction, exactly one commit at the end):

1. ``SELECT status FROM users WHERE id = :user_id FOR UPDATE`` — the SAME
   stable user-level resource the claim flow locks FIRST, so one
   user's claims and abandons share a single serialization queue. This
   is what makes the daily count safe: spec §8.5 requires the concurrent
   daily check to be atomic, and COUNT-then-UPDATE is only sound inside
   that lock (the §8.3 argument, applied to abandons).
2. ``SELECT ... FROM assignment_claims WHERE id = :claim_id FOR UPDATE``
   — the claim row lock. Ownership and status are judged on the row we
   locked, so a concurrent transition of the same claim linearizes here.
3. Daily count inside the user-row lock: this user's ABANDONED claims
   whose ``terminal_at`` instant falls in ``[local-midnight,
   next-local-midnight)`` of BUSINESS_TIMEZONE (see ``business_day_window``).
   The clock instant (``now``) is sampled immediately BEFORE this step —
   after the user-row and claim-row locks are held, before the assignment
   lock — so lock-wait can never skew the daily abandon window, the
   persisted ``terminal_at``, or the audit event's ``occurred_at`` (see
   the CLOCK SAMPLING CONTRACT comment in ``abandon_claim``).
4. Assignment release under FOR UPDATE, then Claim -> ABANDONED +
   ``terminal_at = clock.now()``; flush, publish, one commit.

The lock order (user row -> claim row -> assignment row) keeps the claim
flow's convention of taking the assignment last; claim-side candidates
use FOR UPDATE SKIP LOCKED and never wait on the assignment row, so no
cycle can form between the two services.

Design decisions and rulings:

- **Actionable statuses:** only CLAIMED and REVISION_REQUIRED may be
  abandoned (spec §8.2: those are the states that still need student
  action). VALIDATING/UNDER_REVIEW claims carry an already-submitted
  file the review pipeline owns — abandoning mid-review is not a student
  action, so it is rejected with the registered code
  ``CLAIM_NOT_ABANDONABLE``. COMPLETED/EXPIRED claims are terminal under
  another reason and get the same rejection; §8.5's "已经终态" idempotent
  reply is reserved for replays of a successful ABANDON.
- **Idempotent replay:** re-abandoning an ABANDONED claim returns the
  same terminal row — no error, no recount, no second event (spec §8.5).
  Under concurrency the claim-row lock turns the second caller into
  exactly this replay.
- **Daily window:** ``business_day_window`` converts the Clock's UTC
  instant to BUSINESS_TIMEZONE, takes the local calendar date, and
  returns the two local midnights as UTC instants. DST is handled by
  zoneinfo arithmetic alone — a spring-forward day simply yields a
  23-hour window, a fall-back day a 25-hour one (spec §0/§44.14;
  backend-engineering §11: no hardcoded offsets, zoneinfo only). The
  counter keys on ``terminal_at`` + status ABANDONED, i.e. when the
  abandon happened, never when the claim was made.
- **Release guard:** the assignment flips OCCUPIED -> AVAILABLE only. A
  RETIRED or COMPLETED assignment is sticky (spec §8.2) and is never
  resurrected by an abandon; the claim still reaches ABANDONED so the
  quota and the reassignment exclusion apply regardless.
- **Reassignment exclusion:** ``ClaimService`` already excludes this
  user's ABANDONED assignments from random candidates, which is
  the §8.5 "当前用户后续不得重新随机到同一个 Assignment" guarantee; this
  service only produces the ABANDONED row that feeds it.
- **Behavior history (spec §8.5 写行为历史):** the durable record is the
  claim row itself (status ABANDONED + ``terminal_at``); the audit
  trail is one ``CLAIM_ABANDONED`` DomainEvent published through the
  identity events port (the Core Primitives port; the audit/outbox
  module's AuditService persists it, the in-memory collector serves
  tests) — no schema change required. Like staff_service, the event is
  published after the flush and before the commit; the interim in-memory
  adapter accepts that a failed commit could leave a phantom event, and
  the outbox attaches inside the transaction. It is an audit-stream
  identifier, deliberately NOT a §25 notification event.
- **Account gate:** the abandon refuses non-ACTIVE accounts exactly like
  claiming (defense in depth on the row already locked; real traffic
  cannot reach here with an inactive account because API access gates
  on the same status).
- **Errors:** ABANDON_LIMIT_REACHED and CLAIM_NOT_ABANDONABLE are 409
  (the state-conflict family, matching ASSIGNMENT_LIMIT_REACHED; the
  transport statuses are provisional, mirroring deadlines.py's 400),
  ownership is 403 PERMISSION_DENIED, a missing claim 404, and a missing
  user reuses the claim module's typed errors.
- **Cross-module boundary:** interfaces.md forbids importing identity ORM
  models; the user-row lock goes through the same style of typed
  Core-level ``users`` light table claim_service built (a twin
  definition — claim_service's is private and not republished here; if
  interfaces.md ever registers a locking-read port, both queries
  move behind it together).
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import String, Uuid, column, func, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.enums import UserStatus
from app.modules.identity.events import DomainEvent, DomainEventPublisher
from app.modules.tasks.claim_service import (
    AccountNotActiveError,
    UserNotFoundError,
)
from app.modules.tasks.enums import (
    AssignmentAvailability,
    ClaimStatus,
)
from app.modules.tasks.models import Assignment, AssignmentClaim

__all__ = [
    "ABANDONABLE_STATUSES",
    "CLAIM_ABANDONED",
    "DAILY_ABANDON_LIMIT",
    "AbandonLimitReachedError",
    "AbandonService",
    "ClaimNotAbandonableError",
    "ClaimNotFoundError",
    "ClaimNotOwnedError",
    "business_day_window",
]


# --- frozen value sets and constants ------------------------------------------------


# Spec §8.5: at most 2 abandons per BUSINESS_TIMEZONE natural day. The
# deployment-facing copy is Settings.daily_abandon_limit (the SHOULD-
# configurability clause); the composition root wires the two together.
DAILY_ABANDON_LIMIT = 2

# Spec §8.2: the states that still need student action — the same set the
# claim quota counts (QUOTA_OCCUPYING_STATUSES) — are the states a student
# may walk away from.
ABANDONABLE_STATUSES: tuple[ClaimStatus, ...] = (
    ClaimStatus.CLAIMED,
    ClaimStatus.REVISION_REQUIRED,
)

# Audit-stream identifier for the abandon behavior history (see module
# docstring: audit contract, not the §25 notification event list).
CLAIM_ABANDONED = "CLAIM_ABANDONED"

# Lock/verify seam for the users table (see module docstring): a typed
# Core-level light table, NOT the identity ORM model. Twin of
# claim_service._USERS_LOCK.
_USERS_LOCK = table(
    "users",
    column("id", Uuid),
    column("status", String),
)


# --- messages (§29 envelope text) ----------------------------------------------------


_CLAIM_NOT_FOUND_MESSAGE = "领取记录不存在"
_CLAIM_NOT_OWNED_MESSAGE = "该领取不属于当前用户"
_NOT_ABANDONABLE_MESSAGE = "该领取当前状态不可放弃"
_ABANDON_LIMIT_MESSAGE = "今日放弃次数已达到上限"


# --- typed exceptions (router-mapped) -------------------------------------------------


class ClaimNotFoundError(BusinessError):
    """No AssignmentClaim row for the id (same shape as TaskNotFoundError)."""

    def __init__(self, claim_id: UUID) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            _CLAIM_NOT_FOUND_MESSAGE,
            status_code=404,
            details={"claim_id": str(claim_id)},
        )


class ClaimNotOwnedError(BusinessError):
    """The claim exists but belongs to another user."""

    def __init__(self, claim_id: UUID, user_id: UUID) -> None:
        super().__init__(
            ErrorCode.PERMISSION_DENIED,
            _CLAIM_NOT_OWNED_MESSAGE,
            status_code=403,
            details={"claim_id": str(claim_id), "user_id": str(user_id)},
        )


class ClaimNotAbandonableError(BusinessError):
    """The claim's status is not a student abandon action (spec §8.2/§8.5):
    mid-review (VALIDATING/UNDER_REVIEW) or terminal under another reason
    (COMPLETED/EXPIRED)."""

    def __init__(self, status: ClaimStatus) -> None:
        super().__init__(
            ErrorCode.CLAIM_NOT_ABANDONABLE,
            _NOT_ABANDONABLE_MESSAGE,
            status_code=409,
            details={
                "claim_status": status.value,
                "abandonable_statuses": [
                    abandonable.value for abandonable in ABANDONABLE_STATUSES
                ],
            },
        )


class AbandonLimitReachedError(BusinessError):
    """The student exhausted the daily abandon cap of the current
    BUSINESS_TIMEZONE natural day (spec §8.5)."""

    def __init__(
        self,
        limit: int,
        *,
        window_start_at: datetime,
        window_end_at: datetime,
        business_timezone: str,
    ) -> None:
        super().__init__(
            ErrorCode.ABANDON_LIMIT_REACHED,
            _ABANDON_LIMIT_MESSAGE,
            status_code=409,
            details={
                "limit": limit,
                "window_start_at": window_start_at.isoformat(),
                "window_end_at": window_end_at.isoformat(),
                "business_timezone": business_timezone,
            },
        )


# --- natural-day window (spec §0, §44.14; backend-engineering §11) --------------------


def business_day_window(now: datetime, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    """The [start, end) UTC instants of the BUSINESS_TIMEZONE natural day
    containing ``now``.

    Pure zoneinfo arithmetic — no offsets, no ``timedelta(hours=24)``: the
    window spans from one local midnight to the next, so DST days come out
    23 or 25 hours long exactly as the zone defines them (spec §38.9's
    requirement that nothing be written against +8). Local midnights that
    do not exist in a zone (a DST jump across 00:00) resolve through
    zoneinfo's fold rules, which still yields a well-defined instant pair.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(
            f"now must be a timezone-aware datetime (UTC instant), got naive {now!r}"
        )
    local_date = now.astimezone(timezone).date()
    start = datetime.combine(local_date, time.min, tzinfo=timezone).astimezone(UTC)
    end = datetime.combine(
        local_date + timedelta(days=1), time.min, tzinfo=timezone
    ).astimezone(UTC)
    return start, end


# --- the service ----------------------------------------------------------------------


class AbandonService:
    """Abandon a claim and release its assignment (spec §8.5).

    ``business_timezone`` is the validated Settings string; it becomes a
    ``ZoneInfo`` once here. ``daily_abandon_limit`` is injectable for
    tests (production wires ``Settings.daily_abandon_limit``, whose
    default is the spec §8.5 value of 2).
    """

    def __init__(
        self,
        *,
        clock: Clock,
        business_timezone: str,
        events: DomainEventPublisher,
        daily_abandon_limit: int = DAILY_ABANDON_LIMIT,
    ) -> None:
        if daily_abandon_limit < 1:
            raise ValueError(
                f"daily_abandon_limit must be >= 1, got {daily_abandon_limit}"
            )
        self._clock = clock
        self._timezone = ZoneInfo(business_timezone)
        self._events = events
        self._daily_limit = daily_abandon_limit

    async def abandon_claim(
        self, db: AsyncSession, user_id: UUID, claim_id: UUID
    ) -> AssignmentClaim:
        """Abandon the user's claim: Claim -> ABANDONED, Assignment ->
        AVAILABLE, one audit event, one commit.

        Replaying a successful abandon returns the same terminal row
        without recounting. Raises the typed §8.5 business errors (4xx)
        otherwise; commits exactly once, only on the success path.
        """
        # (1) Same-user serialization FIRST (see module docstring): the
        # stable user-level resource the claim flow also locks, so the
        # daily count below runs under a queue shared with claims.
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

        # (2) Claim row under FOR UPDATE: ownership and status are judged
        # on the locked row.
        claim = await db.scalar(
            select(AssignmentClaim)
            .where(AssignmentClaim.id == claim_id)
            .with_for_update()
        )
        if claim is None:
            raise ClaimNotFoundError(claim_id)
        if claim.user_id != user_id:
            raise ClaimNotOwnedError(claim_id, user_id)

        status = ClaimStatus(claim.status)
        if status is ClaimStatus.ABANDONED:
            # Idempotent replay (spec §8.5): same terminal result, no
            # recount, no event, nothing written.
            return claim
        if status not in ABANDONABLE_STATUSES:
            raise ClaimNotAbandonableError(status)

        # CLOCK SAMPLING CONTRACT (mirrors claim_service):
        # ``now`` is sampled HERE — after every lock the
        # flow takes before its first time consumer (user row FOR UPDATE
        # above, claim row FOR UPDATE above) and before the daily-window
        # computation below. Sampling before the locks would let the
        # user-row lock-wait skew the daily abandon window, the persisted
        # terminal_at, and the audit event's occurred_at backwards by
        # however long the lock was held. A clock advanced between call
        # start and lock acquisition is therefore invisible by
        # construction (asserted by structure: nothing reads self._clock
        # between method entry and this line). FrozenClock tests pin the
        # sampled instant on both sides of this contract.
        now = self._clock.now()

        # (3) Daily cap inside the user-row lock (spec §8.5 atomicity).
        window_start, window_end = business_day_window(now, self._timezone)
        used = await db.scalar(
            select(func.count())
            .select_from(AssignmentClaim)
            .where(
                AssignmentClaim.user_id == user_id,
                AssignmentClaim.status == ClaimStatus.ABANDONED,
                AssignmentClaim.terminal_at >= window_start,
                AssignmentClaim.terminal_at < window_end,
            )
        )
        if (used or 0) >= self._daily_limit:
            raise AbandonLimitReachedError(
                self._daily_limit,
                window_start_at=window_start,
                window_end_at=window_end,
                business_timezone=self._timezone.key,
            )

        # (4) Release the assignment under FOR UPDATE. Only OCCUPIED flips
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

        # (5) Terminal transition + behavior history, one commit.
        claim.status = ClaimStatus.ABANDONED
        claim.terminal_at = now
        await db.flush()
        self._events.publish(
            DomainEvent(
                event_type=CLAIM_ABANDONED,
                aggregate_type="AssignmentClaim",
                aggregate_id=claim.id,
                occurred_at=now,
                payload={
                    "user_id": str(user_id),
                    "assignment_id": str(claim.assignment_id),
                    "task_id": str(claim.task_id),
                    "terminal_at": now.isoformat(),
                },
            )
        )
        await db.commit()
        return claim
