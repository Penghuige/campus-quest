# backend/app/modules/identity/account_admin_service.py
"""Admin account-status governance: suspend/ban/reactivate (Plan 08 T8;
spec §5.7).

Design decisions:

- **The legal-transition table is spec §5.7, verbatim:** ACTIVE ->
  SUSPENDED, ACTIVE -> BANNED, SUSPENDED -> ACTIVE, and BANNED -> ACTIVE
  (the explicit unban). Every other transition — suspending a suspended
  or banned account, banning a suspended account, reactivating an
  already-ACTIVE one, anything from PENDING_PHONE — is a typed 409
  carrying the from/to pair (``InvalidAccountTransitionError``). The
  registry has no dedicated transition-conflict code in this wave, so
  the typed error rides ``VALIDATION_ERROR`` at 409 — the established
  ``StaffService`` 409 shape; a dedicated §29 code is a controller
  registration decision noted in the wave report.
- **Reason is mandatory** (plan Global Constraints: "Admin state repair
  requires reason"): blank-after-strip reasons are a 400 before any
  read, the same validate-before-touch discipline as the staff
  invitation flow.
- **Every committed transition writes its audit row in the SAME
  transaction** (G12; ``AuditLogWriter`` flush-only): action
  ``USER_SUSPENDED``/``USER_BANNED``/``USER_REACTIVATED``, target the
  user, snapshots carry the status migration only (``{"status": ...}`` —
  G11: no nickname/phone/email on the row), plus the free-text reason
  and the ``AuditContext`` correlation pair. A REFUSED transition
  writes nothing (nothing happened — the staff-invitation ruling).
- **History is untouched by design.** The service mutates exactly one
  column of one row (``users.status``) under that row's ``FOR UPDATE``
  lock; ledger, audit, and claim history are not read for mutation and
  not written — pinned by negative assertions in the integration tests.
  Revoking sessions is likewise deliberately NOT done here: plan T8
  step 2 pins enforcement on the per-request service-layer status
  re-checks (claim/upload/community gates), so a still-live access
  token must be refused by those gates, not by session revocation.
- **The row lock serializes concurrent governance:** the user row is
  selected ``FOR UPDATE`` before the transition table is consulted, so
  two admins racing suspend/reactivate on one account linearize and the
  loser sees the typed 409, never a lost update.
- Service-level role gate: every method requires an ADMIN actor
  (``rbac.is_admin``). Routes mount ``require_admin_actor`` on top
  (defense in depth); the BANNED -> ACTIVE unban is Admin-only by the
  same gate, exactly as spec §5.7 phrases it.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import rbac
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.enums import UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User

__all__ = [
    "AUDIT_USER_BANNED",
    "AUDIT_USER_REACTIVATED",
    "AUDIT_USER_SUSPENDED",
    "AccountAdminService",
    "AccountAdminTargetNotFoundError",
    "InvalidAccountTransitionError",
]

logger = logging.getLogger(__name__)

# Durable audit action names (G12; Plan 08 T8), the audit-stream
# vocabulary for this surface (the staff-service precedent: defined
# beside the operations that emit them; interfaces.md registration is
# the controller's step).
AUDIT_USER_SUSPENDED = "USER_SUSPENDED"
AUDIT_USER_BANNED = "USER_BANNED"
AUDIT_USER_REACTIVATED = "USER_REACTIVATED"

_AUDIT_TARGET_TYPE = "user"

_PERMISSION_DENIED_MESSAGE = "仅管理员可以管理账号状态"
_REASON_REQUIRED_MESSAGE = "必须填写操作原因"
_TARGET_NOT_FOUND_MESSAGE = "账号不存在"
_TRANSITION_MESSAGE = "该账号状态不允许此操作"

_SUSPEND_LABEL = "suspend"
_BAN_LABEL = "ban"
_REACTIVATE_LABEL = "reactivate"


class AccountAdminTargetNotFoundError(BusinessError):
    """No User row for the id (the claim-service ``UserNotFoundError``
    shape: typed NOT_FOUND 404, user id in details)."""

    def __init__(self, user_id: UUID) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            _TARGET_NOT_FOUND_MESSAGE,
            status_code=404,
            details={"user_id": str(user_id)},
        )


class InvalidAccountTransitionError(BusinessError):
    """The requested transition is not in spec §5.7's table (typed 409).

    ``details`` carries the observed from-state and the requested
    to-state so the admin sees which rule fired.
    """

    def __init__(self, *, operation: str, current: UserStatus, requested: UserStatus):
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _TRANSITION_MESSAGE,
            status_code=409,
            details={
                "operation": operation,
                "from": current.value,
                "to": requested.value,
            },
        )


class AccountAdminService:
    """Audited account-status transitions, Admin-only (Plan 08 T8).

    ``audit`` defaults to a fresh ``AuditLogWriter`` (stateless,
    flush-only) — the default means the default writer, never "no
    auditing" (the ``StaffService`` wiring ruling).
    """

    def __init__(self, *, audit: AuditLogWriter | None = None) -> None:
        self._audit: AuditLogWriter = audit if audit is not None else AuditLogWriter()

    async def suspend_user(
        self,
        db: AsyncSession,
        actor: Actor,
        user_id: UUID,
        *,
        reason: str,
        audit_context: AuditContext | None = None,
    ) -> User:
        """ACTIVE -> SUSPENDED (spec §5.7)."""
        return await self._transition(
            db,
            actor,
            user_id,
            operation=_SUSPEND_LABEL,
            allowed_from=frozenset({UserStatus.ACTIVE}),
            to_status=UserStatus.SUSPENDED,
            action=AUDIT_USER_SUSPENDED,
            reason=reason,
            audit_context=audit_context,
        )

    async def ban_user(
        self,
        db: AsyncSession,
        actor: Actor,
        user_id: UUID,
        *,
        reason: str,
        audit_context: AuditContext | None = None,
    ) -> User:
        """ACTIVE -> BANNED (spec §5.7)."""
        return await self._transition(
            db,
            actor,
            user_id,
            operation=_BAN_LABEL,
            allowed_from=frozenset({UserStatus.ACTIVE}),
            to_status=UserStatus.BANNED,
            action=AUDIT_USER_BANNED,
            reason=reason,
            audit_context=audit_context,
        )

    async def reactivate_user(
        self,
        db: AsyncSession,
        actor: Actor,
        user_id: UUID,
        *,
        reason: str,
        audit_context: AuditContext | None = None,
    ) -> User:
        """SUSPENDED/BANNED -> ACTIVE — the Admin unban included (§5.7)."""
        return await self._transition(
            db,
            actor,
            user_id,
            operation=_REACTIVATE_LABEL,
            allowed_from=frozenset({UserStatus.SUSPENDED, UserStatus.BANNED}),
            to_status=UserStatus.ACTIVE,
            action=AUDIT_USER_REACTIVATED,
            reason=reason,
            audit_context=audit_context,
        )

    # -- internals -------------------------------------------------------------

    async def _transition(
        self,
        db: AsyncSession,
        actor: Actor,
        user_id: UUID,
        *,
        operation: str,
        allowed_from: frozenset[UserStatus],
        to_status: UserStatus,
        action: str,
        reason: str,
        audit_context: AuditContext | None,
    ) -> User:
        """One audited status transition: gate -> lock -> check -> flip
        -> audit (flush-only) -> one commit (backend-engineering §5)."""
        if not rbac.is_admin(actor.role):
            raise BusinessError(
                ErrorCode.PERMISSION_DENIED,
                _PERMISSION_DENIED_MESSAGE,
                status_code=403,
            )
        # Validate-before-touch: a blank reason must not spend the lock
        # or leak the account's existence (the staff-invitation order).
        if not reason.strip():
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _REASON_REQUIRED_MESSAGE,
                status_code=400,
                details={"field": "reason"},
            )

        user = (
            await db.scalars(select(User).where(User.id == user_id).with_for_update())
        ).first()
        if user is None:
            raise AccountAdminTargetNotFoundError(user_id)
        current = UserStatus(user.status)
        if current not in allowed_from:
            raise InvalidAccountTransitionError(
                operation=operation, current=current, requested=to_status
            )

        user.status = to_status
        await self._audit.append(
            db,
            actor=actor,
            action=action,
            target_type=_AUDIT_TARGET_TYPE,
            target_id=str(user.id),
            reason=reason,
            # G11: the status migration only — no nickname/phone/email.
            before_snapshot={"status": current.value},
            after_snapshot={"status": to_status.value},
            ip_address=audit_context.ip_address if audit_context else None,
            request_id=audit_context.request_id if audit_context else None,
        )
        await db.commit()
        logger.info(
            "account status changed actor_id=%s user_id=%s %s->%s action=%s",
            actor.user_id,
            user.id,
            current.value,
            to_status.value,
            action,
        )
        return user
