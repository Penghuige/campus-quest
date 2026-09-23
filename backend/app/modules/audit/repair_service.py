# backend/app/modules/audit/repair_service.py
"""Named, narrow state-repair commands for operators (Plan 08 T7; spec
§29; quality-gates G7/G8; Review Focus 5).

**What this module is:** two ADMIN-only commands that repair DERIVED
operational state that a crash left dangling — an Assignment still
OCCUPIED after its Claim reached a release-terminal status, and a
notification delivery wedged in SENDING. Both require a free-text
reason, both write their audit row in the SAME transaction as the
repair (``AuditLogWriter`` flush-only; one commit — the
``AccountAdminService`` discipline), and both snapshot only business
facts (G11: statuses, never PII).

**What this module structurally is NOT (plan T7 step 2):** a generic
repair surface. The service offers exactly the two named commands —
there is no method that deletes Claim history, rewrites a Ledger row's
amount, deletes Submission review history, or executes caller-supplied
SQL, and no command accepts a table name, column name, or raw SQL:
every parameter is a typed UUID / enum / string literal that becomes a
BOUND parameter (the typed signature is the proof; the integration
tests pin the exact public surface and the refusal of forged
parameters). Immutable history is not a permission check away from
deletion — the delete path does not exist to permit (Review Focus 5:
forced state repair must not delete/rewrite immutable
Claim/Submission/Ledger history).

**G7 boundary:** PostgreSQL business facts (Ledger rows, Claim history,
Submission review records) are never touched here; the commands repair
only derived availability/dispatch state, and the audit row records the
before/after so the repair itself is history.

**Module-boundary note:** ``audit/service.py`` (the append-only
writer) deliberately imports nothing from the domain modules — the
arrow points IN. This file is the plan's OPERATIONS surface filed
under the audit package (T7's file list), not part of the writer: it
imports the tasks and notifications ORM models the way any
orchestrating service would, while the writer's boundary is unchanged.

**``release_occupied_assignment`` semantics.** ``Assignment`` availability
is derived state the claim service maintains; the dangling shape is an
assignment still OCCUPIED whose occupancy is no longer backed by an
ACTIVE claim (models.py: OCCUPIED is held by one of the four active
claim statuses — an ABANDONED/EXPIRED claim releases it, a COMPLETED
claim graduates the assignment to COMPLETED). The command releases to
AVAILABLE exactly when NO active claim exists for the assignment and
its newest claim is release-terminal (ABANDONED/EXPIRED) or there is no
claim at all (an occupancy with no backing claim can only arise from
out-of-band DB surgery; restoring the invariant is the repair). It
refuses with a typed 409 when the assignment is not OCCUPIED (nothing
to repair), an active claim still backs the occupancy (the legitimate
state), or the newest claim is COMPLETED — the sticky family
(§8.2: a COMPLETED assignment never returns to AVAILABLE), whose
correct repair is a different, not-yet-built command.

Serialization: the assignment row is locked FOR UPDATE before the
claim facts are read, and every occupancy-changing flow takes the same
row lock (claiming selects candidates FOR UPDATE SKIP LOCKED — a
locked assignment is skipped, never waited on; abandon/expiry/
completion lock the assignment row), so the repair's view of
"active claim or not" cannot race a concurrent state change.

**cleanup-claim fencing boundary (report item):** the file-retention
deletion claim guards SUBMISSION-side protection transitions
(``submissions.cleanup_claim``). This command mutates exactly one
ASSIGNMENT row and touches no submission row, so the fence is not on
its write path; any future repair that does write submission rows MUST
pass ``ensure_no_active_cleanup_claim`` first. Stated here so the
boundary is a contract, not an accident.

**``force_fail_delivery`` semantics.** The manual backstop for a
delivery wedged in SENDING (a sender that died between its claim
commit and its finalize; V1 lease semantics are the ``updated_at``
timestamp heuristic — delivery_service). The command flips SENDING ->
FAILED with a ``"forced:"`` last_error annotation; every other status
is a typed 409 (PENDING/RETRYABLE still have scheduled work; SENT/
FAILED are terminal). The automatic counterpart is the
``workers.recover_stuck_sending`` monitor (W5a carry), which flips
aged SENDING rows to RETRYABLE for re-dispatch: the monitor is the
automatic path, this command is the operator's manual kill — they land
different states and never conflict, and the delivery service's
finalize gate (``status != SENDING`` -> the forced outcome wins)
arbitrates any race with a live sender, whose late finalize is
reported as the observed terminal state. The write stamps
``updated_at`` from the injected Clock — the column's lease time
domain is the SERVICE clock (models.py: no ORM onupdate), and a repair
must not mix a database instant into it.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import rbac
from app.core.clock import Clock, SystemClock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.events import Actor
from app.modules.notifications.enums import DeliveryStatus
from app.modules.notifications.models import NotificationDelivery
from app.modules.tasks.enums import AssignmentAvailability, ClaimStatus
from app.modules.tasks.models import ACTIVE_CLAIM_STATUSES, Assignment, AssignmentClaim

__all__ = [
    "AUDIT_STATE_REPAIR_FAILED_DELIVERY",
    "AUDIT_STATE_REPAIR_RELEASED_ASSIGNMENT",
    "AssignmentOccupancyNotDanglingError",
    "DeliveryNotStuckSendingError",
    "FORCED_LAST_ERROR_PREFIX",
    "RepairService",
    "RepairTargetNotFoundError",
]

logger = logging.getLogger(__name__)

# Durable audit action names (G12; Plan 08 T7), the audit-stream
# vocabulary for this surface. interfaces.md registration is the
# controller's step (the W5a report carries the registration list).
AUDIT_STATE_REPAIR_RELEASED_ASSIGNMENT = "STATE_REPAIR_RELEASED_ASSIGNMENT"
AUDIT_STATE_REPAIR_FAILED_DELIVERY = "STATE_REPAIR_FAILED_DELIVERY"

#: The last_error token a forced failure records, so an admin failure
#: query can tell an operator kill from a provider failure family
#: ("temporary:"/"unknown_outcome:"/"permanent:"/"skipped:",
#: delivery_service) at a glance.
FORCED_LAST_ERROR_PREFIX = "forced:"

_AUDIT_TARGET_ASSIGNMENT = "assignment"
_AUDIT_TARGET_DELIVERY = "notification_delivery"

_PERMISSION_DENIED_MESSAGE = "仅管理员可以执行状态修复"
_REASON_REQUIRED_MESSAGE = "必须填写操作原因"
_ID_REQUIRED_MESSAGE = "目标标识必须是合法的 UUID"
_TARGET_NOT_FOUND_MESSAGE = "修复目标不存在"
_ASSIGNMENT_NOT_DANGLING_MESSAGE = "该任务占用量不是悬挂状态，无需修复"
_DELIVERY_NOT_STUCK_MESSAGE = "该通知投递不在卡死（SENDING）状态"

# The release family: terminal claim statuses whose correct follow-up
# is Assignment -> AVAILABLE (models.py). COMPLETED is deliberately
# absent — the sticky family whose repair target is COMPLETED, not
# AVAILABLE (§8.2).
_RELEASE_TERMINAL_CLAIM_STATUSES = frozenset(
    {ClaimStatus.ABANDONED, ClaimStatus.EXPIRED}
)


class RepairTargetNotFoundError(BusinessError):
    """No row for the typed id (the ``AccountAdminTargetNotFoundError``
    shape: typed NOT_FOUND 404, target id in details)."""

    def __init__(self, target_type: str, target_id: UUID) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            _TARGET_NOT_FOUND_MESSAGE,
            status_code=404,
            details={"target_type": target_type, "target_id": str(target_id)},
        )


class AssignmentOccupancyNotDanglingError(BusinessError):
    """The occupancy is legitimate or absent — release is refused (typed
    409; the ``InvalidAccountTransitionError`` typed-409 precedent).

    ``details`` carries the observed assignment availability, the
    backing claim's status when one exists, and the refusal reason
    token, so the operator sees which rule fired.
    """

    def __init__(
        self,
        *,
        availability: AssignmentAvailability,
        claim_status: ClaimStatus | None,
        why: str,
    ) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _ASSIGNMENT_NOT_DANGLING_MESSAGE,
            status_code=409,
            details={
                "availability_status": availability.value,
                "claim_status": claim_status.value
                if claim_status is not None
                else None,
                "why": why,
            },
        )


class DeliveryNotStuckSendingError(BusinessError):
    """The delivery is not in the SENDING wedge force-fail exists for
    (typed 409). PENDING/RETRYABLE still have scheduled work; SENT and
    FAILED are terminal — none is an operator-kill target."""

    def __init__(self, *, current: DeliveryStatus) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _DELIVERY_NOT_STUCK_MESSAGE,
            status_code=409,
            details={"current_status": current.value},
        )


class RepairService:
    """The two named state-repair commands (see the module docstring):
    Admin-only, reason-mandatory, audited in the repair's transaction.

    ``audit`` defaults to a fresh ``AuditLogWriter`` (stateless,
    flush-only — the default means the default writer, never "no
    auditing"); ``clock`` defaults to ``SystemClock`` and exists so the
    SENDING-lease timestamp domain stays single (tests freeze it).
    """

    def __init__(
        self, *, clock: Clock | None = None, audit: AuditLogWriter | None = None
    ) -> None:
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._audit: AuditLogWriter = audit if audit is not None else AuditLogWriter()

    async def release_occupied_assignment(
        self,
        db: AsyncSession,
        actor: Actor,
        assignment_id: UUID,
        *,
        reason: str,
        audit_context: AuditContext | None = None,
    ) -> Assignment:
        """Put a dangling OCCUPIED Assignment back to AVAILABLE (Plan 08
        T7 step 1). Mutates exactly one assignment row; Claim history is
        read, never written."""
        _require_admin(actor)
        _require_reason(reason)
        assignment_id = _require_uuid(assignment_id, "assignment_id")

        assignment = (
            await db.scalars(
                select(Assignment)
                .where(Assignment.id == assignment_id)
                .with_for_update()
            )
        ).first()
        if assignment is None:
            raise RepairTargetNotFoundError(_AUDIT_TARGET_ASSIGNMENT, assignment_id)

        availability = AssignmentAvailability(assignment.availability_status)
        if availability is not AssignmentAvailability.OCCUPIED:
            # Nothing to repair: the dangling shape is defined on OCCUPIED.
            raise AssignmentOccupancyNotDanglingError(
                availability=availability, claim_status=None, why="not_occupied"
            )

        active_claim_status = await db.scalar(
            select(AssignmentClaim.status)
            .where(
                AssignmentClaim.assignment_id == assignment_id,
                AssignmentClaim.status.in_(
                    [status.value for status in ACTIVE_CLAIM_STATUSES]
                ),
            )
            .limit(1)
        )
        if active_claim_status is not None:
            # The occupancy is backed by a live claim: the legitimate
            # state, not a repair target.
            raise AssignmentOccupancyNotDanglingError(
                availability=availability,
                claim_status=ClaimStatus(active_claim_status),
                why="active_claim_exists",
            )

        newest_claim = (
            await db.scalars(
                select(AssignmentClaim)
                .where(AssignmentClaim.assignment_id == assignment_id)
                .order_by(AssignmentClaim.claimed_at.desc(), AssignmentClaim.id.desc())
                .limit(1)
            )
        ).first()
        if newest_claim is None:
            # OCCUPIED with no claim at all: only out-of-band surgery
            # produces this; restoring the invariant is the repair.
            claim_status: ClaimStatus | None = None
            claim_id: UUID | None = None
        else:
            claim_status = ClaimStatus(newest_claim.status)
            claim_id = newest_claim.id
            if claim_status not in _RELEASE_TERMINAL_CLAIM_STATUSES:
                # COMPLETED (the sticky family): the correct repair is
                # COMPLETED, not AVAILABLE — a different, later command.
                raise AssignmentOccupancyNotDanglingError(
                    availability=availability,
                    claim_status=claim_status,
                    why="claim_status_not_release_terminal",
                )

        assignment.availability_status = AssignmentAvailability.AVAILABLE.value
        await self._audit.append(
            db,
            actor=actor,
            action=AUDIT_STATE_REPAIR_RELEASED_ASSIGNMENT,
            target_type=_AUDIT_TARGET_ASSIGNMENT,
            target_id=str(assignment.id),
            reason=reason,
            # G11: availability facts only, plus the claim state in
            # details (the module docstring's snapshot contract).
            before_snapshot={
                "availability_status": AssignmentAvailability.OCCUPIED.value
            },
            after_snapshot={
                "availability_status": AssignmentAvailability.AVAILABLE.value
            },
            details={
                "claim_id": str(claim_id) if claim_id is not None else None,
                "claim_status": claim_status.value
                if claim_status is not None
                else None,
                "task_id": str(assignment.task_id),
            },
            ip_address=audit_context.ip_address if audit_context else None,
            request_id=audit_context.request_id if audit_context else None,
        )
        await db.commit()
        logger.info(
            "state repair released assignment actor_id=%s assignment_id=%s "
            "claim_status=%s",
            actor.user_id,
            assignment.id,
            claim_status.value if claim_status is not None else None,
        )
        return assignment

    async def force_fail_delivery(
        self,
        db: AsyncSession,
        actor: Actor,
        delivery_id: UUID,
        *,
        reason: str,
        audit_context: AuditContext | None = None,
    ) -> NotificationDelivery:
        """Fail a wedged SENDING delivery (the manual stuck-SENDING
        backstop). Mutates exactly one delivery row's dispatch state;
        the notification message and its history are untouched."""
        _require_admin(actor)
        _require_reason(reason)
        delivery_id = _require_uuid(delivery_id, "delivery_id")

        delivery = (
            await db.scalars(
                select(NotificationDelivery)
                .where(NotificationDelivery.id == delivery_id)
                .with_for_update()
            )
        ).first()
        if delivery is None:
            raise RepairTargetNotFoundError(_AUDIT_TARGET_DELIVERY, delivery_id)

        current = DeliveryStatus(delivery.status)
        if current is not DeliveryStatus.SENDING:
            raise DeliveryNotStuckSendingError(current=current)

        delivery.status = DeliveryStatus.FAILED.value
        delivery.last_error = f"{FORCED_LAST_ERROR_PREFIX}{reason}"
        # The lease column's time domain is the service clock (models.py
        # has no ORM onupdate on it); the repair stamps the injected
        # clock, never the database instant.
        delivery.updated_at = self._clock.now()
        await self._audit.append(
            db,
            actor=actor,
            action=AUDIT_STATE_REPAIR_FAILED_DELIVERY,
            target_type=_AUDIT_TARGET_DELIVERY,
            target_id=str(delivery.id),
            reason=reason,
            before_snapshot={"status": DeliveryStatus.SENDING.value},
            after_snapshot={"status": DeliveryStatus.FAILED.value},
            details={
                "channel": delivery.channel,
                "attempts": delivery.attempts,
                "last_error": delivery.last_error,
            },
            ip_address=audit_context.ip_address if audit_context else None,
            request_id=audit_context.request_id if audit_context else None,
        )
        await db.commit()
        logger.info(
            "state repair failed delivery actor_id=%s delivery_id=%s attempts=%s",
            actor.user_id,
            delivery.id,
            delivery.attempts,
        )
        return delivery


# -- shared gates ---------------------------------------------------------------------


def _require_admin(actor: Actor) -> None:
    """Service-level role gate; routes mount ``require_admin_actor`` on
    top (defense in depth — the ``AccountAdminService`` ruling)."""
    if not rbac.is_admin(actor.role):
        raise BusinessError(
            ErrorCode.PERMISSION_DENIED,
            _PERMISSION_DENIED_MESSAGE,
            status_code=403,
        )


def _require_reason(reason: str) -> None:
    """Validate-before-touch: a blank reason must not spend the lock or
    leak the target's existence (plan Global Constraints: "Admin state
    repair requires reason")."""
    if not reason.strip():
        raise BusinessError(
            ErrorCode.VALIDATION_ERROR,
            _REASON_REQUIRED_MESSAGE,
            status_code=400,
            details={"field": "reason"},
        )


def _require_uuid(value: UUID | str, field: str) -> UUID:
    """Coerce the typed id, refusing anything that is not a UUID.

    This is the runtime half of "no table/column/raw-SQL parameters": a
    forged string (a table name, a SQL fragment) never becomes a query
    fragment — it is rejected as a 400 before any read, and even a
    parseable id only ever feeds a BOUND parameter equality.
    """
    if isinstance(value, UUID):
        return value
    try:
        return UUID(value)
    except (TypeError, ValueError) as exc:
        raise BusinessError(
            ErrorCode.VALIDATION_ERROR,
            _ID_REQUIRED_MESSAGE,
            status_code=400,
            details={"field": field},
        ) from exc
