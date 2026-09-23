# backend/app/modules/points/admin_service.py
"""Admin operations on the points domain: RewardItem catalogue
administration, the scoped Teacher review authorization, and the manual
points adjustment channel (Plan 08 T4 + T6; spec §4.3, §15, §16).

Design decisions:

- **Catalogue changes are future-bound by the snapshot discipline
  (plan 08 T4 step 2).** Editing ``point_cost`` / ``per_user_term_limit``
  (or stock) only changes what FUTURE redemption requests see: every
  open ``RewardRedemption`` keeps its request-time ``points`` snapshot
  and its ACTIVE ``PointReservation`` keeps the frozen amount — the
  update paths here never touch redemption or reservation rows, and the
  integration tests pin that with negative assertions. Nothing about an
  update needs to "migrate" open requests; the schema did the work at
  request time.
- **The scoped delegation ruling (the wave's core decision).** The PR
  #2 hardening interim guard ("Admin-only until scoped delegation
  lands") is lifted by giving Admins a grantable, revocable, AUDITED
  global authorization: ``reward_review_grants`` (migration 0021 — the
  TaskCollaborator capability vocabulary is DB-CHECK-closed, so the
  extension needs its own table with the same closed-CHECK discipline;
  the ruling's "no migration" hope died on that grep, see the wave
  report). Scope is **V1-GLOBAL**: a RewardRedemption carries no
  course/task dimension, so the plan's "configured course/task scope"
  has no join key to bind a grant to — inventing one (e.g. per-task
  grants for a global object) would be a silent product-semantics
  change (G13). A per-scope refinement is an owner ruling away: the
  guard reads ONE predicate (``has_reward_review_grant``), and widening
  that predicate is the only seam.
- **Grants are current state; audit is the history.** Row EXISTS =
  authorized; revocation is a DELETE, so the guard's uncached per-request
  read makes revocation effective on the very next call. The
  REWARD_REVIEW_GRANTED/_REVOKED audit rows carry the who/why the row
  cannot (G12) — the projection-vs-fact split every other table here
  follows. Reason is mandatory on both (the account-status governance
  precedent: authority changes need their why recorded).
- **Every committed admin change writes its audit row in the SAME
  transaction** (G12; the flush-only ``AuditLogWriter`` discipline):
  catalogue rows carry before/after snapshots of the CHANGED fields
  only (G11: business fields — costs, stock, windows — never customer
  PII, which these tables do not hold anyway); refused and replayed
  calls write nothing.
- **``admin_adjust_points`` goes through ``LedgerService.post_entry``
  and NEVER a direct wallet UPDATE** (plan 08 T6 step 3; spec §15 积分
  禁止直改). The entry is one ``ADMIN_ADJUSTMENT`` row:
  ``affects_balance=true``, ``affects_ranking=false`` FIXED — spec
  §15/§17.1 say "Admin Adjustment 默认 affects_ranking = false" and
  define NO explicit ranking-affecting adjustment operation (the only
  ranking-affecting admin channel the spec names is the assignment-
  reward reversal, §17.2, already implemented), so review focus 4's
  "unless an explicit supported ranking-affecting operation is chosen"
  resolves to: none exists, ranking can never move through this
  channel. An optional ranking mode would need an owner ruling plus a
  period-attribution rule; the tests pin the isolation.
- **The adjustment is idempotent by ``operation_id``** (PR #5
  final-review fix A, P1; G8 at-least-once needs business idempotency):
  the caller's stable intent UUID is the entry's ``source_id``, so
  ``UNIQUE(source_type, source_id, ledger_type)`` makes one operation
  id one ledger row — a response lost to a network blip and retried
  with the SAME id replays the ORIGINAL entry (no second row, no
  second wallet move, no second audit row) instead of double-charging.
  A replay carrying a DIFFERENT canonical intent (user/amount/reason)
  under a used id is the typed 409 CONFLICT: the id names a decision,
  and re-deciding under it would launder the audit trail. The
  savepoint-bounded insert keeps a genuine concurrent same-id race on
  the same recovery path (the grant_reward_review /
  grant_assignment_reward precedent).
- **The adjustment's audit snapshots read under the wallet row lock**
  (the reversal discipline): the wallet is locked through the module's
  one ``locked_or_created_wallet`` before the before-read, so a
  concurrent balance-moving post cannot slip between the snapshot and
  the mutation — the audited migration is the TRUE locked-row
  transition. A negative adjustment may overdraft ``available_points``
  below zero (the migration-0012 ruling: the manual correction channel
  is exactly the case that rule was written for).
- Service-level role gate on every method (``rbac.is_admin``): the HTTP
  surfaces mount admin guards on top (T9 wires them); direct service
  callers get the same typed 403 (defense in depth, the account-admin
  precedent).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import rbac
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.directory import SqlAlchemyUserDirectory, UserDirectory
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.points.enums import LedgerType
from app.modules.points.ledger_service import (
    InvalidLedgerEntryError,
    LedgerService,
    PostLedgerEntry,
)
from app.modules.points.models import PointsLedger, RewardItem, RewardReviewGrant
from app.modules.points.redemption_service import (
    REWARD_REVIEW_CAPABILITY,
    RewardItemNotFoundError,
)

__all__ = [
    "AUDIT_ADMIN_POINTS_ADJUSTED",
    "AUDIT_REWARD_ITEM_CREATED",
    "AUDIT_REWARD_ITEM_DISABLED",
    "AUDIT_REWARD_ITEM_UPDATED",
    "AUDIT_REWARD_REVIEW_GRANTED",
    "AUDIT_REWARD_REVIEW_REVOKED",
    "AdminReasonRequiredError",
    "DuplicateRewardReviewGrantError",
    "PointsAdminService",
    "PointsAdjustmentOperationConflictError",
    "PointsAdjustmentTargetNotFoundError",
    "RewardAdminService",
    "RewardItemChanges",
    "RewardItemValidationError",
    "RewardReviewGrantNotFoundError",
    "PointsAdjustmentTargetNotFoundError",
    "RewardReviewGrantLine",
    "UNSET",
]

logger = logging.getLogger(__name__)

# Durable audit action names (G12; Plan 08 T4/T6) — the audit-stream
# vocabulary for this surface, defined beside the operations that emit
# them (the account-admin precedent; interfaces.md registration is the
# controller's step, recorded in the wave report).
AUDIT_REWARD_ITEM_CREATED = "REWARD_ITEM_CREATED"
AUDIT_REWARD_ITEM_UPDATED = "REWARD_ITEM_UPDATED"
AUDIT_REWARD_ITEM_DISABLED = "REWARD_ITEM_DISABLED"
AUDIT_REWARD_REVIEW_GRANTED = "REWARD_REVIEW_GRANTED"
AUDIT_REWARD_REVIEW_REVOKED = "REWARD_REVIEW_REVOKED"
AUDIT_ADMIN_POINTS_ADJUSTED = "ADMIN_POINTS_ADJUSTED"

# The audit target types: catalogue rows and ledger-backed actions on a
# user's balance; grants target the TEACHER account (the brief's
# "target=teacher").
_REWARD_ITEM_TARGET = "reward_item"
_USER_TARGET = "user"

_PERMISSION_DENIED_MESSAGE = "只有管理员可以管理奖品目录、审阅授权与积分调整"
_REASON_REQUIRED_MESSAGE = "必须填写操作原因"
_EMPTY_UPDATE_MESSAGE = "没有提供任何要修改的字段"
_ITEM_SHAPE_MESSAGE = "奖品配置不合法"
_UNKNOWN_TARGET_MESSAGE = "指定的教师账号不存在"
_NOT_TEACHER_MESSAGE = "审阅授权只能授予教师账号"
_DUPLICATE_GRANT_MESSAGE = "该教师已持有兑换审阅授权"
_GRANT_MISSING_MESSAGE = "该教师未持有兑换审阅授权"
_ADJUST_TARGET_MISSING_MESSAGE = "积分调整目标账号不存在"
_OPERATION_ID_CONFLICT_MESSAGE = "该操作 ID 已用于不同的积分调整"

# The PK a racing second grant loses to (models.py); only its violation
# is translated into the typed duplicate conflict (backend-engineering
# §7: only the expected constraint is converted).
_GRANT_PK = "pk_reward_review_grants"

# The ledger's idempotency constraint (models.py; ledger_service's
# _SOURCE_TRIPLE_UQ): a same-operation_id race loses to exactly this
# one, and only its violation takes the replay path.
_SOURCE_TRIPLE_UQ = "uq_points_ledger_source_type_source_id_ledger_type"


# --- the update command -------------------------------------------------


class _Unset:
    """The "field not provided" marker (distinct from ``None``, which is
    a meaningful value for the nullable catalogue columns)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return "UNSET"


UNSET: Final[_Unset] = _Unset()


@dataclass(frozen=True, slots=True)
class RewardItemChanges:
    """A partial RewardItem update: every field defaults to UNSET
    ("leave unchanged"), and ``None`` remains a real value ("clear the
    window bound / make stock unlimited"). ``enabled`` is deliberately
    absent — switching an item off is the dedicated, reason-requiring
    ``disable_reward_item`` operation with its own audit action."""

    name: str | _Unset = UNSET
    description: str | None | _Unset = UNSET
    point_cost: int | _Unset = UNSET
    stock: int | None | _Unset = UNSET
    per_user_term_limit: int | None | _Unset = UNSET
    available_from: datetime | None | _Unset = UNSET
    available_until: datetime | None | _Unset = UNSET
    requires_manual_review: bool | _Unset = UNSET
    fulfillment_instructions: str | None | _Unset = UNSET

    def provided(self) -> dict[str, object]:
        """``{field: value}`` for exactly the provided fields, in
        declaration order (deterministic snapshots)."""
        return {
            field.name: getattr(self, field.name)
            for field in fields(self)
            if getattr(self, field.name) is not UNSET
        }


# --- typed exceptions (router-mapped) -----------------------------------


class RewardItemValidationError(BusinessError):
    """A catalogue shape the database CHECKs would also refuse, answered
    friendly-first (backend-engineering §6)."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            message,
            status_code=422,
            details={"field": field},
        )


class DuplicateRewardReviewGrantError(BusinessError):
    """A live grant row already exists for the Teacher (the
    DuplicateCollaboratorError shape: VALIDATION_ERROR carried at 409 —
    no dedicated registry code exists for grant conflicts)."""

    def __init__(self, teacher_id: UUID) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _DUPLICATE_GRANT_MESSAGE,
            status_code=409,
            details={"teacher_id": str(teacher_id)},
        )


class RewardReviewGrantNotFoundError(BusinessError):
    """No live grant row for the Teacher on revocation (the
    CollaboratorNotFoundError shape)."""

    def __init__(self, teacher_id: UUID) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            _GRANT_MISSING_MESSAGE,
            status_code=404,
            details={"teacher_id": str(teacher_id)},
        )


class AdminReasonRequiredError(BusinessError):
    """An adjustment without a non-blank reason (spec §15: the reason IS
    the correction's audit trail; blank is not a reason)."""

    def __init__(self) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _REASON_REQUIRED_MESSAGE,
            status_code=400,
            details={"field": "reason"},
        )


class PointsAdjustmentTargetNotFoundError(BusinessError):
    """No account for the target user id (the claim-service 404 shape)."""

    def __init__(self, user_id: UUID) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            _ADJUST_TARGET_MISSING_MESSAGE,
            status_code=404,
            details={"user_id": str(user_id)},
        )


class PointsAdjustmentOperationConflictError(BusinessError):
    """The operation id was already spent on a DIFFERENT adjustment (PR
    #5 fix A, P1): a replay must carry the same canonical intent — the
    id is the decision's name, not a slot to re-decide in."""

    def __init__(self, operation_id: UUID) -> None:
        super().__init__(
            ErrorCode.CONFLICT,
            _OPERATION_ID_CONFLICT_MESSAGE,
            status_code=409,
            details={"operation_id": str(operation_id)},
        )


# --- shared internals ----------------------------------------------------


def _require_admin(actor: Actor) -> None:
    """The service-level role gate every admin operation enters through
    (defense in depth under the transport guards; the account-admin
    precedent)."""
    if not rbac.is_admin(actor.role):
        raise BusinessError(
            ErrorCode.PERMISSION_DENIED,
            _PERMISSION_DENIED_MESSAGE,
            status_code=403,
        )


def _require_reason(reason: str | None) -> str:
    """The stripped reason, or the typed 400 — validate-before-touch so a
    blank reason spends no lock and leaks no target existence."""
    reason_text = reason.strip() if isinstance(reason, str) else ""
    if not reason_text:
        raise AdminReasonRequiredError()
    return reason_text


def _replayed_adjustment(
    existing: PointsLedger,
    *,
    operation_id: UUID,
    user_id: UUID,
    amount: int,
    reason_text: str,
) -> PointsLedger:
    """The replay verdict for an already-applied operation id (G8): the
    SAME canonical intent (target user, amount, trimmed reason — the
    fields the caller's decision fixed) returns the ORIGINAL entry and
    writes nothing; anything else is the typed 409 — an operation id
    names one decision, and a different decision under it would launder
    the audit trail."""
    if (
        existing.user_id == user_id
        and existing.amount == amount
        and (existing.reason or "") == reason_text
    ):
        logger.info(
            "admin points adjustment replay operation_id=%s returns the "
            "existing entry %s (nothing written)",
            operation_id,
            existing.id,
        )
        return existing
    raise PointsAdjustmentOperationConflictError(operation_id)


def _snapshot_value(value: object) -> object:
    """One snapshot-ready value: datetimes become ISO strings (the
    JSONB payload rule), everything else rides as-is."""
    return value.isoformat() if isinstance(value, datetime) else value


def _validate_catalog_shape(
    *,
    name: str,
    point_cost: int,
    stock: int | None,
    per_user_term_limit: int | None,
    available_from: datetime | None,
    available_until: datetime | None,
) -> None:
    """The friendly mirror of the reward_items CHECKs (spec §16 field
    rules); the database constraints remain the backstop."""
    if not name.strip():
        raise RewardItemValidationError("name", _ITEM_SHAPE_MESSAGE)
    if point_cost <= 0:
        raise RewardItemValidationError("point_cost", _ITEM_SHAPE_MESSAGE)
    if stock is not None and stock < 0:
        raise RewardItemValidationError("stock", _ITEM_SHAPE_MESSAGE)
    if per_user_term_limit is not None and per_user_term_limit < 0:
        raise RewardItemValidationError("per_user_term_limit", _ITEM_SHAPE_MESSAGE)
    if (
        available_from is not None
        and available_until is not None
        and available_from >= available_until
    ):
        raise RewardItemValidationError("available_from", _ITEM_SHAPE_MESSAGE)


# --- the services --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RewardReviewGrantLine:
    """One live-grant row for the admin grants listing (T10's query
    gap-fill): the grant facts from the row plus the teacher's DISPLAY
    nickname resolved through the frozen directory port — the module
    boundary holds on reads too (account facts never through identity
    ORM). ``nickname`` is ``None`` only if the port cannot resolve the
    display profile (no such edge in V1: accounts have no delete
    path)."""

    teacher_id: UUID
    nickname: str | None
    granted_by: UUID
    granted_at: datetime


class RewardAdminService:
    """RewardItem catalogue administration plus the reviewer-grant
    lifecycle (Plan 08 T4).

    ``directory`` is the frozen identity port (module boundary: account
    facts only through the port, never identity ORM); ``audit`` defaults
    to a fresh ``AuditLogWriter`` — the default means the default
    writer, never "no auditing" (the StaffService wiring ruling)."""

    def __init__(
        self,
        *,
        directory: UserDirectory | None = None,
        audit: AuditLogWriter | None = None,
    ) -> None:
        self._directory: UserDirectory = (
            directory if directory is not None else SqlAlchemyUserDirectory()
        )
        self._audit: AuditLogWriter = audit if audit is not None else AuditLogWriter()

    # -- catalogue: create ----------------------------------------------------

    async def create_reward_item(
        self,
        db: AsyncSession,
        actor: Actor,
        *,
        name: str,
        point_cost: int,
        description: str | None = None,
        stock: int | None = None,
        per_user_term_limit: int | None = None,
        available_from: datetime | None = None,
        available_until: datetime | None = None,
        requires_manual_review: bool = False,
        fulfillment_instructions: str | None = None,
        reason: str | None = None,
        audit_context: AuditContext | None = None,
    ) -> RewardItem:
        """Insert one enabled RewardItem (spec §16: 价格、库存、学期上限
        全部由 Admin 配置) and audit the creation.

        The new row starts ENABLED — taking an item off the shelf is the
        dedicated ``disable_reward_item`` transition. ``reason`` is
        optional here (catalogue content management); the audit row
        carries it when given.
        """
        _require_admin(actor)
        _validate_catalog_shape(
            name=name,
            point_cost=point_cost,
            stock=stock,
            per_user_term_limit=per_user_term_limit,
            available_from=available_from,
            available_until=available_until,
        )
        item = RewardItem(
            name=name.strip(),
            description=description,
            point_cost=point_cost,
            stock=stock,
            per_user_term_limit=per_user_term_limit,
            available_from=available_from,
            available_until=available_until,
            requires_manual_review=requires_manual_review,
            fulfillment_instructions=fulfillment_instructions,
        )
        db.add(item)
        await db.flush()  # server id for the audit target
        after = {
            "name": item.name,
            "point_cost": item.point_cost,
            "stock": item.stock,
            "per_user_term_limit": item.per_user_term_limit,
            "available_from": _snapshot_value(item.available_from),
            "available_until": _snapshot_value(item.available_until),
            "requires_manual_review": item.requires_manual_review,
            "enabled": item.enabled,
        }
        await self._audit.append(
            db,
            actor=actor,
            action=AUDIT_REWARD_ITEM_CREATED,
            target_type=_REWARD_ITEM_TARGET,
            target_id=str(item.id),
            reason=reason.strip()
            if isinstance(reason, str) and reason.strip()
            else None,
            after_snapshot=after,
            ip_address=audit_context.ip_address if audit_context else None,
            request_id=audit_context.request_id if audit_context else None,
        )
        await db.commit()
        logger.info(
            "reward item created actor_id=%s item_id=%s", actor.user_id, item.id
        )
        return item

    # -- catalogue: update ------------------------------------------------------

    async def update_reward_item(
        self,
        db: AsyncSession,
        actor: Actor,
        item_id: UUID,
        changes: RewardItemChanges,
        *,
        reason: str | None = None,
        audit_context: AuditContext | None = None,
    ) -> RewardItem:
        """Apply a partial update under the item row lock.

        Cost/limit/stock edits bind FUTURE requests only (the snapshot
        ruling — see the module docstring); open redemptions and their
        ACTIVE reservations are never read for mutation here. The audit
        before/after snapshots carry exactly the CHANGED fields.
        """
        _require_admin(actor)
        provided = changes.provided()
        if not provided:
            raise RewardItemValidationError("changes", _EMPTY_UPDATE_MESSAGE)

        item = await db.scalar(
            select(RewardItem)
            .where(RewardItem.id == item_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if item is None:
            raise RewardItemNotFoundError(item_id)

        # Validate the MERGED row before mutating: a window bound edited
        # alone is checked against the surviving other bound, and a
        # refused update leaves the locked row untouched (validate
        # before touch, backend-engineering §6).
        merged = {
            "name": provided.get("name", item.name),
            "point_cost": provided.get("point_cost", item.point_cost),
            "stock": provided.get("stock", item.stock),
            "per_user_term_limit": provided.get(
                "per_user_term_limit", item.per_user_term_limit
            ),
            "available_from": provided.get("available_from", item.available_from),
            "available_until": provided.get("available_until", item.available_until),
        }
        _validate_catalog_shape(**merged)  # type: ignore[arg-type]

        before = {field: _snapshot_value(getattr(item, field)) for field in provided}
        for field, value in provided.items():
            setattr(item, field, value)
        after = {field: _snapshot_value(value) for field, value in provided.items()}
        await self._audit.append(
            db,
            actor=actor,
            action=AUDIT_REWARD_ITEM_UPDATED,
            target_type=_REWARD_ITEM_TARGET,
            target_id=str(item.id),
            reason=reason.strip()
            if isinstance(reason, str) and reason.strip()
            else None,
            before_snapshot=before,
            after_snapshot=after,
            ip_address=audit_context.ip_address if audit_context else None,
            request_id=audit_context.request_id if audit_context else None,
        )
        await db.commit()
        logger.info(
            "reward item updated actor_id=%s item_id=%s fields=%s",
            actor.user_id,
            item.id,
            sorted(provided),
        )
        return item

    # -- catalogue: disable -----------------------------------------------------

    async def disable_reward_item(
        self,
        db: AsyncSession,
        actor: Actor,
        item_id: UUID,
        *,
        reason: str,
        audit_context: AuditContext | None = None,
    ) -> RewardItem:
        """enabled -> false (下架), reason mandatory (the governance
        precedent — taking a reward off the shelf needs its why).

        A replay on an already-disabled item is the idempotent no-op
        that returns the row and writes NOTHING (the redemption-decision
        discipline: the first application's audit row is the decision's
        trace).
        """
        _require_admin(actor)
        reason_text = _require_reason(reason)

        item = await db.scalar(
            select(RewardItem)
            .where(RewardItem.id == item_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if item is None:
            raise RewardItemNotFoundError(item_id)
        if not item.enabled:
            await db.commit()  # idempotent replay: release the lock
            return item

        item.enabled = False
        await self._audit.append(
            db,
            actor=actor,
            action=AUDIT_REWARD_ITEM_DISABLED,
            target_type=_REWARD_ITEM_TARGET,
            target_id=str(item.id),
            reason=reason_text,
            before_snapshot={"enabled": True},
            after_snapshot={"enabled": False},
            ip_address=audit_context.ip_address if audit_context else None,
            request_id=audit_context.request_id if audit_context else None,
        )
        await db.commit()
        logger.info(
            "reward item disabled actor_id=%s item_id=%s", actor.user_id, item.id
        )
        return item

    # -- reviewer authorization: the scoped delegation ---------------------------

    async def grant_reward_review(
        self,
        db: AsyncSession,
        actor: Actor,
        teacher_id: UUID,
        *,
        reason: str,
        audit_context: AuditContext | None = None,
    ) -> RewardReviewGrant:
        """Grant the global REWARD_REVIEW authorization to a Teacher.

        Target must be a TEACHER account (verified through the identity
        directory port — the collaborator-service boundary); a live
        grant for the account is the typed duplicate conflict (the row
        IS the grant, PK teacher_id). The grant takes effect on the
        guard's next read — nothing else to invalidate.
        """
        _require_admin(actor)
        reason_text = _require_reason(reason)

        target_role = await self._directory.get_role(db, teacher_id)
        if target_role is None:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _UNKNOWN_TARGET_MESSAGE,
                status_code=400,
                details={"teacher_id": str(teacher_id)},
            )
        if target_role is not Role.TEACHER:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _NOT_TEACHER_MESSAGE,
                status_code=400,
                details={"teacher_id": str(teacher_id), "role": target_role.value},
            )

        existing = await db.get(RewardReviewGrant, teacher_id)
        if existing is not None:
            raise DuplicateRewardReviewGrantError(teacher_id)

        grant = RewardReviewGrant(
            teacher_id=teacher_id,
            capability=REWARD_REVIEW_CAPABILITY,
            granted_by=actor.user_id,
        )
        db.add(grant)
        try:
            await db.flush()
        except IntegrityError as exc:
            # Only the PK race is expected (the role/duplicate checks
            # preceded); anything else propagates as an unknown
            # database failure (backend-engineering §7).
            if _GRANT_PK not in str(exc):
                raise
            raise DuplicateRewardReviewGrantError(teacher_id) from exc
        await self._audit.append(
            db,
            actor=actor,
            action=AUDIT_REWARD_REVIEW_GRANTED,
            target_type=_USER_TARGET,
            target_id=str(teacher_id),
            reason=reason_text,
            after_snapshot={"capability": REWARD_REVIEW_CAPABILITY},
            ip_address=audit_context.ip_address if audit_context else None,
            request_id=audit_context.request_id if audit_context else None,
        )
        await db.commit()
        logger.info(
            "reward review granted actor_id=%s teacher_id=%s",
            actor.user_id,
            teacher_id,
        )
        return grant

    async def revoke_reward_review(
        self,
        db: AsyncSession,
        actor: Actor,
        teacher_id: UUID,
        *,
        reason: str,
        audit_context: AuditContext | None = None,
    ) -> None:
        """Revoke the authorization: DELETE the grant row.

        Effective immediately — the review guard reads the table per
        request with no cache, so the teacher's very next call fails.
        Grant history stays in the audit stream (REWARD_REVIEW_GRANTED
        row + this one). Revoking an absent grant is the typed 404 (the
        remove-collaborator shape).
        """
        _require_admin(actor)
        reason_text = _require_reason(reason)

        grant = await db.get(RewardReviewGrant, teacher_id)
        if grant is None:
            raise RewardReviewGrantNotFoundError(teacher_id)
        await db.delete(grant)
        await self._audit.append(
            db,
            actor=actor,
            action=AUDIT_REWARD_REVIEW_REVOKED,
            target_type=_USER_TARGET,
            target_id=str(teacher_id),
            reason=reason_text,
            before_snapshot={"capability": grant.capability},
            ip_address=audit_context.ip_address if audit_context else None,
            request_id=audit_context.request_id if audit_context else None,
        )
        await db.commit()
        logger.info(
            "reward review revoked actor_id=%s teacher_id=%s",
            actor.user_id,
            teacher_id,
        )

    async def list_reward_review_grants(
        self,
        db: AsyncSession,
        actor: Actor,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[RewardReviewGrantLine], int]:
        """The live-grant page, newest grant first (T10's admin query
        gap-fill): who currently holds the REWARD_REVIEW authorization,
        who granted it, and when — the grant row is current state; the
        who/why HISTORY stays in the audit stream.

        The teacher's display nickname resolves through the frozen
        directory port per row (a page is capped at the transport's 50,
        so at most 50 port reads — no batch seam the port does not
        offer). Pagination bounds are the CALLER's (the transport's
        family cap); read-only, commits nothing."""
        _require_admin(actor)
        total = int(
            await db.scalar(select(func.count()).select_from(RewardReviewGrant))
        )
        grants = await db.scalars(
            select(RewardReviewGrant)
            .order_by(RewardReviewGrant.granted_at.desc(), RewardReviewGrant.teacher_id)
            .limit(limit)
            .offset(offset)
        )
        lines: list[RewardReviewGrantLine] = []
        for grant in grants:
            profile = await self._directory.get_display_profile(db, grant.teacher_id)
            lines.append(
                RewardReviewGrantLine(
                    teacher_id=grant.teacher_id,
                    nickname=profile.nickname if profile is not None else None,
                    granted_by=grant.granted_by,
                    granted_at=grant.granted_at,
                )
            )
        return lines, total


class PointsAdminService:
    """The manual points adjustment channel (Plan 08 T6; spec §15 人工
    积分调整).

    ``ledger`` defaults to a fresh ``LedgerService`` (stateless); the
    constructor seam exists so callers can bind the ranking-projection
    dispatcher for telemetry — the adjustment itself is ranking-neutral
    and arms nothing. ``directory`` is the target-existence port.
    """

    def __init__(
        self,
        *,
        ledger: LedgerService | None = None,
        audit: AuditLogWriter | None = None,
        directory: UserDirectory | None = None,
    ) -> None:
        self._ledger = ledger if ledger is not None else LedgerService()
        self._audit: AuditLogWriter = audit if audit is not None else AuditLogWriter()
        self._directory: UserDirectory = (
            directory if directory is not None else SqlAlchemyUserDirectory()
        )

    async def admin_adjust_points(
        self,
        db: AsyncSession,
        actor: Actor,
        user_id: UUID,
        amount: int,
        *,
        reason: str,
        operation_id: UUID,
        audit_context: AuditContext | None = None,
    ) -> PointsLedger:
        """Post one ADMIN_ADJUSTMENT entry through the ledger and audit
        the wallet migration in the SAME transaction.

        Ranking isolation (plan 08 T6 step 1 / review focus 4): the
        entry is ``affects_balance=True, affects_ranking=False`` — the
        wallet moves, the daily/monthly/all boards do not (spec §17.1;
        there is no supported ranking-affecting adjustment operation,
        see the module docstring). Zero amounts are the ledger's typed
        422; the wallet may overdraft negative on a downward correction
        (the migration-0012 ruling). Commits exactly once.

        Idempotency (PR #5 fix A, P1; G8): ``operation_id`` is the
        caller's stable intent id and becomes the entry's
        ``source_id``. A replay with the SAME id and the SAME canonical
        intent (user, amount, trimmed reason) returns the ORIGINAL
        entry and writes nothing — the retry after a lost response
        cannot double-charge. The same id with a DIFFERENT intent is
        the typed 409 ``CONFLICT``. A genuine concurrent same-id race
        loses to ``UNIQUE(source_type, source_id, ledger_type)`` inside
        a savepoint and recovers onto the same replay path.
        """
        _require_admin(actor)
        reason_text = _require_reason(reason)
        # Friendly-first (backend-engineering §6): the zero-amount gate
        # is the ledger's own typed 422, answered BEFORE the replay
        # lookup, the directory read, and the wallet lock — post_entry's
        # _validate stays the backstop.
        if amount == 0:
            raise InvalidLedgerEntryError("amount", "积分流水金额必须是非零整数")

        # The replay lookup (G8): the operation id IS the ledger's
        # source id, so an already-applied adjustment answers from the
        # fact, spending no wallet lock and writing nothing.
        existing = await db.scalar(self._adjustment_filter(operation_id))
        if existing is not None:
            return _replayed_adjustment(
                existing,
                operation_id=operation_id,
                user_id=user_id,
                amount=amount,
                reason_text=reason_text,
            )

        target_role = await self._directory.get_role(db, user_id)
        if target_role is None:
            raise PointsAdjustmentTargetNotFoundError(user_id)

        # The reversal discipline: lock the wallet row through the
        # module's ONE wallet lock BEFORE reading the balance, so the
        # audited before/after is the TRUE locked-row transition
        # (post_entry re-takes the lock harmlessly inside this
        # transaction).
        wallet = await self._ledger.locked_or_created_wallet(db, user_id)
        balance_before = wallet.available_points

        try:
            # The savepoint bounds the same-operation_id race loser's
            # damage: the UNIQUE violation aborts only this insert,
            # leaving the transaction usable for the recovery read (the
            # grant_assignment_reward precedent).
            async with db.begin_nested():
                entry = await self._ledger.post_entry(
                    db,
                    PostLedgerEntry(
                        user_id=user_id,
                        ledger_type=LedgerType.ADMIN_ADJUSTMENT,
                        amount=amount,
                        source_type=LedgerType.ADMIN_ADJUSTMENT.value,
                        # The caller's stable intent id — the triple's
                        # idempotency key (models.py; PR #5 fix A).
                        source_id=operation_id,
                        affects_balance=True,
                        affects_ranking=False,
                        operator_id=actor.user_id,
                        reason=reason_text,
                    ),
                )
        except IntegrityError as exc:
            if _SOURCE_TRIPLE_UQ not in str(exc):
                raise  # unknown database failure, not our idempotency race
            # The wallet lock serialized us behind the winner's commit,
            # so the row is visible now. Expire savepoint-scoped state
            # (the never-inserted pending entry, the wallet snapshot)
            # before re-reading, then answer through the replay path.
            db.expire_all()
            winner = await db.scalar(self._adjustment_filter(operation_id))
            if winner is None:
                raise
            await db.commit()  # release the wallet lock; nothing was written
            return _replayed_adjustment(
                winner,
                operation_id=operation_id,
                user_id=user_id,
                amount=amount,
                reason_text=reason_text,
            )
        balance_after = wallet.available_points
        await self._audit.append(
            db,
            actor=actor,
            action=AUDIT_ADMIN_POINTS_ADJUSTED,
            target_type=_USER_TARGET,
            target_id=str(user_id),
            reason=reason_text,
            before_snapshot={"available_points": balance_before},
            after_snapshot={
                "available_points": balance_after,
                "amount": entry.amount,
                "ledger_entry_id": str(entry.id),
            },
            ip_address=audit_context.ip_address if audit_context else None,
            request_id=audit_context.request_id if audit_context else None,
        )
        await db.commit()
        logger.info(
            "admin points adjusted actor_id=%s user_id=%s amount=%s operation_id=%s",
            actor.user_id,
            user_id,
            amount,
            operation_id,
        )
        return entry

    @staticmethod
    def _adjustment_filter(operation_id: UUID) -> Select[tuple[PointsLedger]]:
        """The one-per-operation adjustment lookup: the operation id as
        the ADMIN_ADJUSTMENT source triple — the UNIQUE constraint's
        read-side twin (the _claim_reward_filter shape)."""
        return select(PointsLedger).where(
            PointsLedger.source_type == LedgerType.ADMIN_ADJUSTMENT.value,
            PointsLedger.source_id == operation_id,
            PointsLedger.ledger_type == LedgerType.ADMIN_ADJUSTMENT.value,
        )
