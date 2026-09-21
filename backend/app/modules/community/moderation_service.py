# backend/app/modules/community/moderation_service.py
"""Admin identity reveal for anonymous comments (spec §21.4; plan 06
task 8).

Design decisions:

- **The reveal is the one explicit identity surface.** Every moderation
  read stays pseudonymous — 匿名用户 plus the keyed ``moderation_key``
  (``serializers.derive_moderation_key``) — including the Admin's own
  queue listings; identity appears only through
  ``request_identity_reveal``, which exists precisely so that looking
  is a decision, not a render.
- **Admin-only (spec §4.3/§21.4).** Teachers — task owners and
  MODERATE_COMMUNITY collaborators included — and Students are the
  typed ``RevealDeniedError`` wall: community governance is the
  Teacher's, de-anonymization is the Admin's. Standing is judged on the
  Actor's server-resolved role, never a client-supplied value.
- **Reason mandatory, typed (spec §21.4 每次追溯).** A reveal without a
  reason — None, a non-string, or blank after trimming — is the typed
  VALIDATION_ERROR raised before any database touch (the
  category-first precedent). The audited reason is the trimmed text.
- **Every call audits — durably (G12; PR #2 hardening P0-5).**
  Re-revealing is allowed (repeat tracing is legitimate) and EVERY
  call — first or repeat — writes one ``COMMUNITY_IDENTITY_REVEAL``
  row into ``audit_logs`` (actor, comment target, reason, database
  timestamp, plus the disclosed ``revealed_user_id`` in ``details``)
  through the flush-only ``AuditLogWriter`` and publishes one
  ``COMMENT_IDENTITY_REVEALED`` DomainEvent through the injected
  publisher port. V1 writes the audit row DIRECTLY in this
  transaction (controller ruling: no event-consumer pipeline — Plan
  08's audit query/UI work may build one); the DomainEvent stream is
  unchanged for the future pipeline.
- **Governance reaches history.** The comment lookup is a plain
  existence read — soft-deleted and hard-hidden comments stay
  revealable (the privacy/legal removal flow is exactly when tracing
  matters), unlike the public-surface visibility gate.
- **The response carries the real identity TO THE ADMIN.**
  ``RevealedIdentity`` (schemas) — user id, nickname, username (the
  student number) — is the documented explicit-reveal surface and must
  never be composed into a student-facing or moderation-list response.
- **One write, one commit.** The audit row is the reveal's only
  persistence: written and committed here (backend-engineering §5 —
  the service owns its transaction). The comment row itself is still
  untouched — the reveal changes no comment state.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import String, Uuid, column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, SystemClock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.rbac import is_admin
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.community.gates import (
    CommenterNotFoundError,
    CommentNotFoundError,
)
from app.modules.community.models import Comment
from app.modules.community.schemas import RevealedIdentity
from app.modules.identity.events import (
    Actor,
    DomainEvent,
    DomainEventPublisher,
    LoggingEventPublisher,
)

__all__ = [
    "COMMENT_IDENTITY_REVEALED",
    "COMMUNITY_IDENTITY_REVEAL",
    "ModerationService",
    "RevealDeniedError",
    "RevealReasonRequiredError",
    "normalize_reveal_reason",
]

# Audit action name (the STAFF_INVITATION_CREATED family): the durable
# audit_logs row's action for the identity reveal (G12; PR #2 hardening
# P0-5) — distinct from the DomainEvent type below (the event-stream
# identifier), which the future audit pipeline keeps consuming.
COMMUNITY_IDENTITY_REVEAL = "COMMUNITY_IDENTITY_REVEAL"

# The audit target vocabulary for the reveal: the comment whose author
# was de-anonymized (target_id = the comment's UUID as text).
_AUDIT_TARGET_TYPE = "comment"

# Audit event name (the STAFF_INVITATION_CREATED family): consumed by
# the audit/outbox module's AuditLog (spec §21.4 每次追溯必须写 AuditLog).
COMMENT_IDENTITY_REVEALED = "COMMENT_IDENTITY_REVEALED"

# Identity seam (the T2/gates precedent): a typed Core-level light
# users table, NOT the identity ORM model — username (the student
# number on the reveal surface) and nickname for the reveal response.
_USERS = table(
    "users",
    column("id", Uuid),
    column("username", String),
    column("nickname", String),
)

# --- messages (§29 envelope text) ---------------------------------------------------

_REASON_REQUIRED_MESSAGE = "必须填写追溯原因"
_REVEAL_DENIED_MESSAGE = "只有管理员可以追溯匿名评论作者身份"


# --- typed exceptions (router-mapped) ------------------------------------------------


class RevealReasonRequiredError(BusinessError):
    """``reason`` is None, not a string, or blank after trimming — spec
    §21.4 makes the reason a mandatory part of every trace, so a
    reasonless reveal is rejected before anything is read or audited."""

    def __init__(self) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _REASON_REQUIRED_MESSAGE,
            status_code=400,
        )


class RevealDeniedError(BusinessError):
    """The actor is not Admin (spec §4.3/§21.4) — Teachers and Students
    never de-anonymize; the moderation surfaces they reach stay
    pseudonymous."""

    def __init__(self, role: object) -> None:
        super().__init__(
            ErrorCode.PERMISSION_DENIED,
            _REVEAL_DENIED_MESSAGE,
            status_code=403,
            details={"role": str(role)},
        )


# --- input normalization --------------------------------------------------------------


def normalize_reveal_reason(reason: str) -> str:
    """Trim the reason; missing, non-string, or blank-after-trim is the
    typed rejection. Pure — the check runs before any database touch
    (the normalize_report_note precedent)."""
    if not isinstance(reason, str) or not reason.strip():
        raise RevealReasonRequiredError()
    return reason.strip()


# --- the service ----------------------------------------------------------------------


class ModerationService:
    """The Admin identity-reveal operation (see module docstring): one
    method, one wall, one audit stream per call."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        events: DomainEventPublisher | None = None,
        audit: AuditLogWriter | None = None,
    ) -> None:
        # The clock is the only business-time source (the event's
        # occurred_at); the publisher is the audit outbox port — the
        # in-memory collector in tests, the log-and-nothing adapter in
        # interim production (the CommentService wiring); the audit
        # writer is the durable audit_logs seam — stateless and
        # flush-only, defaulting to a fresh instance so no wiring slip
        # can silently drop the G12 trace.
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._events: DomainEventPublisher = (
            events if events is not None else LoggingEventPublisher()
        )
        self._audit: AuditLogWriter = audit if audit is not None else AuditLogWriter()

    async def request_identity_reveal(
        self,
        db: AsyncSession,
        admin_actor: Actor,
        comment_id: UUID,
        reason: str,
        *,
        audit_context: AuditContext | None = None,
    ) -> RevealedIdentity:
        """Reveal ``comment_id``'s author to an Admin, audited every
        call — one durable ``audit_logs`` row and one DomainEvent per
        call (spec §21.4 每次追溯必须写 AuditLog).

        Admin-only (the server-resolved role; teachers and students are
        the typed wall), reason mandatory (typed rejection, trimmed for
        the audit payload), comment by plain existence — removed
        comments stay revealable (governance reaches history). Returns
        ``RevealedIdentity`` — the real identity, TO THIS ADMIN ONLY.
        Repeats are allowed; each call writes its own audit row and
        event, and the transaction commits here (the audit row is the
        reveal's only write; the comment row is untouched).

        The reveal is an ACCESS action: it migrates no state, so the
        §30 before/after snapshots stay NULL (0016) and the audit
        context's ip/request_id columns carry where the look came from.
        """
        if not is_admin(admin_actor.role):
            raise RevealDeniedError(admin_actor.role)
        normalized_reason = normalize_reveal_reason(reason)

        comment = await db.scalar(select(Comment).where(Comment.id == comment_id))
        if comment is None:
            raise CommentNotFoundError(comment_id)

        row = (
            await db.execute(
                select(_USERS.c.username, _USERS.c.nickname).where(
                    _USERS.c.id == comment.user_id
                )
            )
        ).first()
        if row is None:
            # The FK holds on every write path, so this is the account
            # vanishing underneath a surviving comment: stop at the
            # shared typed error rather than fabricate an identity.
            raise CommenterNotFoundError(comment.user_id)
        username, nickname = row

        self._events.publish(
            DomainEvent(
                event_type=COMMENT_IDENTITY_REVEALED,
                aggregate_type="Comment",
                aggregate_id=comment.id,
                occurred_at=self._clock.now(),
                payload={
                    "actor_user_id": str(admin_actor.user_id),
                    "comment_id": str(comment.id),
                    "task_id": str(comment.task_id),
                    "reason": normalized_reason,
                    "revealed_user_id": str(comment.user_id),
                },
            )
        )
        # The durable G12 trace: flush-only append, then THIS service
        # commits it (the reveal's only write; backend-engineering §5).
        # The disclosed identity rides ``details.revealed_user_id`` as
        # an id — the nickname/username NEVER land on the audit row
        # (G11: the audit trail proves WHO looked and AT WHAT, not a
        # second copy of what they saw).
        await self._audit.append(
            db,
            actor=admin_actor,
            action=COMMUNITY_IDENTITY_REVEAL,
            target_type=_AUDIT_TARGET_TYPE,
            target_id=str(comment.id),
            reason=normalized_reason,
            details={
                "task_id": str(comment.task_id),
                "revealed_user_id": str(comment.user_id),
            },
            ip_address=audit_context.ip_address if audit_context else None,
            request_id=audit_context.request_id if audit_context else None,
        )
        await db.commit()
        return RevealedIdentity(
            user_id=comment.user_id,
            # Row unpacks arrive as Any; both columns are NOT NULL VARCHAR.
            nickname=str(nickname),
            username=str(username),
        )
