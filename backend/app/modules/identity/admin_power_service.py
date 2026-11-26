# backend/app/modules/identity/admin_power_service.py
"""Admin console read projections and emergency session-revocation use cases.

- ``user_panorama``: read-only account summary (profile, wallet balance, recent
  submissions, active sessions/devices) using Core light tables for foreign
  domains so identity never imports foreign ORM models.
- ``kick``: revokes all active refresh sessions and drops remembered-device
  credentials in one transaction with an ``ADMIN_SESSIONS_REVOKED`` audit row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypedDict
from uuid import UUID

from sqlalchemy import DateTime, Integer, String, Uuid, column, func, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity import device_credentials
from app.modules.identity.account_admin_service import (
    AccountAdminTargetNotFoundError as AdminTargetNotFoundError,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import RememberedDevice, User, UserSession
from app.modules.identity.session_service import SessionService

if TYPE_CHECKING:
    import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

AUDIT_ADMIN_PASSWORD_RESET_ISSUED = "ADMIN_PASSWORD_RESET_ISSUED"
AUDIT_ADMIN_PASSWORD_RESET_CONSUMED = "ADMIN_PASSWORD_RESET_CONSUMED"
#: The reset link's storage: SHA-256 of the urlsafe32 plaintext -> user
#: id, TTL 48h (Redis expiry IS the link's lifetime — no sweep needed).
_RESET_KEY_PREFIX = "adminpwreset:"
RESET_TOKEN_TTL_SECONDS = 48 * 3600
_RESET_TOKEN_INVALID_MESSAGE = "重置链接无效或已过期"

AUDIT_ADMIN_SESSIONS_REVOKED = "ADMIN_SESSIONS_REVOKED"
_RECENT_SUBMISSIONS_LIMIT = 10

# Core light tables for read-only admin console projections so identity never
# imports ORM models from points, submissions, or tasks.
_POINT_WALLETS = table(
    "point_wallets",
    column("user_id", Uuid),
    column("available_points", Integer),
)
_REWARD_REDEMPTIONS = table(
    "reward_redemptions",
    column("status", String),
)
_SUBMISSIONS = table(
    "submissions",
    column("id", Uuid),
    column("claim_id", Uuid),
    column("validation_status", String),
    column("review_status", String),
    column("submitted_at", DateTime(timezone=True)),
)
_ASSIGNMENT_CLAIMS = table(
    "assignment_claims",
    column("id", Uuid),
    column("user_id", Uuid),
    column("task_id", Uuid),
)
_TASKS = table(
    "tasks",
    column("id", Uuid),
    column("title", String),
    column("status", String),
)


@dataclass(frozen=True, slots=True)
class RecentSubmissionSummary:
    """Read-only submission summary row for the admin user panorama."""

    id: UUID
    validation_status: str
    review_status: str
    submitted_at: datetime


class AdminPanorama(TypedDict):
    """The §3.1 facts, typed for the route's DTO assembly."""

    user: User
    wallet_balance: int
    recent_submissions: list[tuple[RecentSubmissionSummary, str]]
    active_session_count: int
    remembered_device_count: int


async def user_panorama(
    db: AsyncSession, user_id: UUID, *, clock: Clock
) -> AdminPanorama:
    """Every §3.1 fact for one account, as a plain dict for the route's
    explicit DTO. Raises AdminTargetNotFoundError for an unknown id."""
    user = await db.get(User, user_id)
    if user is None:
        raise AdminTargetNotFoundError(user_id)

    wallet_balance = await db.scalar(
        select(_POINT_WALLETS.c.available_points).where(
            _POINT_WALLETS.c.user_id == user_id
        )
    )
    recent = [
        (
            RecentSubmissionSummary(
                id=row.id,
                validation_status=row.validation_status,
                review_status=row.review_status,
                submitted_at=row.submitted_at,
            ),
            row.task_title,
        )
        for row in (
            await db.execute(
                select(
                    _SUBMISSIONS.c.id,
                    _SUBMISSIONS.c.validation_status,
                    _SUBMISSIONS.c.review_status,
                    _SUBMISSIONS.c.submitted_at,
                    _TASKS.c.title.label("task_title"),
                )
                .select_from(
                    _SUBMISSIONS.join(
                        _ASSIGNMENT_CLAIMS,
                        _SUBMISSIONS.c.claim_id == _ASSIGNMENT_CLAIMS.c.id,
                    ).join(_TASKS, _ASSIGNMENT_CLAIMS.c.task_id == _TASKS.c.id)
                )
                .where(_ASSIGNMENT_CLAIMS.c.user_id == user_id)
                .order_by(_SUBMISSIONS.c.submitted_at.desc(), _SUBMISSIONS.c.id)
                .limit(_RECENT_SUBMISSIONS_LIMIT)
            )
        ).all()
    ]
    now = clock.now()
    active_sessions = int(
        await db.scalar(
            select(func.count())
            .select_from(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
            )
        )
        or 0
    )
    devices = int(
        await db.scalar(
            select(func.count())
            .select_from(RememberedDevice)
            .where(
                RememberedDevice.user_id == user_id,
                RememberedDevice.expires_at > now,
            )
        )
        or 0
    )
    return {
        "user": user,
        "wallet_balance": int(wallet_balance) if wallet_balance is not None else 0,
        "recent_submissions": recent,
        "active_session_count": active_sessions,
        "remembered_device_count": devices,
    }


async def kick(
    db: AsyncSession,
    actor: Actor,
    user_id: UUID,
    *,
    sessions: SessionService,
    clock: Clock,
    audit: AuditLogWriter | None = None,
    audit_context: AuditContext | None = None,
) -> tuple[int, int]:
    """Revoke every live session and device credential of one account.

    Returns (revoked_sessions, revoked_devices). The audit row's
    snapshot carries the two counts — governance facts only (G11)."""
    user = await db.get(User, user_id)
    if user is None:
        raise AdminTargetNotFoundError(user_id)

    now = clock.now()
    revoked_sessions = int(
        await db.scalar(
            select(func.count())
            .select_from(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
            )
        )
        or 0
    )
    await sessions.revoke_all(db, user_id)
    revoked_devices = await device_credentials.revoke_all(db, user_id)

    writer = audit if audit is not None else AuditLogWriter()
    await writer.append(
        db,
        actor=actor,
        action=AUDIT_ADMIN_SESSIONS_REVOKED,
        target_type="user",
        target_id=str(user_id),
        after_snapshot={
            "revoked_sessions": revoked_sessions,
            "revoked_devices": revoked_devices,
        },
        ip_address=audit_context.ip_address if audit_context else None,
        request_id=audit_context.request_id if audit_context else None,
    )
    await db.commit()
    logger.info(
        "admin kicked user_id=%s sessions=%d devices=%d by=%s",
        user_id,
        revoked_sessions,
        revoked_devices,
        actor.user_id,
    )
    return revoked_sessions, revoked_devices


def _hash_reset_token(token: str) -> str:
    from hashlib import sha256

    return sha256(token.encode()).hexdigest()


async def issue_password_reset(
    db: AsyncSession,
    actor: Actor,
    user_id: UUID,
    *,
    redis: aioredis.Redis,
    clock: Clock,
    audit: AuditLogWriter | None = None,
    audit_context: AuditContext | None = None,
) -> str:
    """Mint the §3.2 one-time reset link for ANOTHER staff account.

    The urlsafe32 plaintext is returned exactly once (the console shows
    it; only its SHA-256 is stored, with a 48h TTL); ISSUED is audited
    with the target as the row's target_id and no credential material
    in the snapshot (G11). Self-service is refused — an admin resets
    others, never their own authentication (the separation is the
    control)."""
    import secrets as _secrets

    if user_id == actor.user_id:
        raise BusinessError(
            ErrorCode.PERMISSION_DENIED,
            "不能为自己签发密码重置链接",
            status_code=403,
        )
    user = await db.get(User, user_id)
    if user is None:
        raise AdminTargetNotFoundError(user_id)
    if user.role == Role.STUDENT.value:
        # Students recover through their bound-phone OTP flow (§5.6).
        raise BusinessError(
            ErrorCode.PERMISSION_DENIED,
            "学生账号使用手机验证码重置流程",
            status_code=403,
        )

    token = _secrets.token_urlsafe(32)
    await redis.set(
        _RESET_KEY_PREFIX + _hash_reset_token(token),
        str(user_id),
        ex=RESET_TOKEN_TTL_SECONDS,
    )
    writer = audit if audit is not None else AuditLogWriter()
    await writer.append(
        db,
        actor=actor,
        action=AUDIT_ADMIN_PASSWORD_RESET_ISSUED,
        target_type="user",
        target_id=str(user_id),
        after_snapshot={"ttl_hours": 48},
        ip_address=audit_context.ip_address if audit_context else None,
        request_id=audit_context.request_id if audit_context else None,
    )
    await db.commit()
    logger.info("admin reset link issued target=%s by=%s", user_id, actor.user_id)
    return token


async def consume_password_reset(
    db: AsyncSession,
    *,
    redis: aioredis.Redis,
    reset_token: str,
    new_password: str,
    clock: Clock,
    sessions: SessionService,
    audit: AuditLogWriter | None = None,
) -> None:
    """Spend the §3.2 link: set the new password, revoke every live
    session, write CONSUMED. The credential is single-use (GETDEL); an
    unknown/expired/spent token answers the uniform invalid — the spend
    endpoint is public, the token IS the authorization."""
    from app.core.security import hash_password, validate_password_length

    user_id_raw = await redis.getdel(_RESET_KEY_PREFIX + _hash_reset_token(reset_token))
    if not isinstance(user_id_raw, str):
        raise BusinessError(
            ErrorCode.OTP_TOKEN_INVALID,
            _RESET_TOKEN_INVALID_MESSAGE,
            status_code=400,
        )
    user = await db.get(User, UUID(user_id_raw))
    if user is None or user.status != UserStatus.ACTIVE.value:
        raise BusinessError(
            ErrorCode.OTP_TOKEN_INVALID,
            _RESET_TOKEN_INVALID_MESSAGE,
            status_code=400,
        )
    validate_password_length(new_password)
    user.password_hash = hash_password(new_password)
    await sessions.revoke_all(db, user.id)
    writer = audit if audit is not None else AuditLogWriter()
    await writer.append(
        db,
        actor=Actor(user_id=user.id, role=Role(user.role)),
        action=AUDIT_ADMIN_PASSWORD_RESET_CONSUMED,
        target_type="user",
        target_id=str(user.id),
        after_snapshot={"sessions_revoked": True},
    )
    await db.commit()
    logger.info("admin reset link consumed user_id=%s", user.id)


AUDIT_ROLE_CHANGED = "ROLE_CHANGED"
_ROLE_CHANGE_REFUSED_LAST_ADMIN = "不能降级最后一个活跃管理员"


async def change_role(
    db: AsyncSession,
    actor: Actor,
    user_id: UUID,
    new_role: Role,
    *,
    audit: AuditLogWriter | None = None,
    audit_context: AuditContext | None = None,
) -> User:
    """The §3.4 TEACHER<->ADMIN flip: never on oneself, never on a
    student, never leaving zero ACTIVE ADMINs.

    Two guards: the sequential one here (count under the target's row
    lock -> the friendly 409), and the DB trigger (0031) re-adjudicating
    every UPDATE under FOR UPDATE on the whole ACTIVE-ADMIN set — the
    concurrent backstop. ROLE_CHANGED audits before/after."""
    if user_id == actor.user_id:
        raise BusinessError(
            ErrorCode.PERMISSION_DENIED, "不能修改自己的角色", status_code=403
        )
    user = await db.get(User, user_id)
    if user is None:
        raise AdminTargetNotFoundError(user_id)
    if user.role == Role.STUDENT.value:
        raise BusinessError(
            ErrorCode.PERMISSION_DENIED,
            "学生账号不参与角色变更",
            status_code=403,
        )
    if new_role not in (Role.TEACHER, Role.ADMIN):
        raise BusinessError(
            ErrorCode.VALIDATION_ERROR,
            "角色只能在 TEACHER 与 ADMIN 之间切换",
            status_code=400,
        )
    old_role = Role(user.role)
    if old_role == new_role:
        return user

    locked = await db.scalar(select(User).where(User.id == user_id).with_for_update())
    if locked is None:
        raise AdminTargetNotFoundError(user_id)
    if old_role is Role.ADMIN and new_role is Role.TEACHER:
        others = int(
            await db.scalar(
                select(func.count())
                .select_from(User)
                .where(
                    User.role == Role.ADMIN.value,
                    User.status == UserStatus.ACTIVE.value,
                    User.id != user_id,
                )
            )
            or 0
        )
        if others == 0:
            raise BusinessError(
                ErrorCode.CONFLICT,
                _ROLE_CHANGE_REFUSED_LAST_ADMIN,
                status_code=409,
            )
    locked.role = new_role.value
    await db.flush()
    writer = audit if audit is not None else AuditLogWriter()
    await writer.append(
        db,
        actor=actor,
        action=AUDIT_ROLE_CHANGED,
        target_type="user",
        target_id=str(user_id),
        before_snapshot={"role": old_role.value},
        after_snapshot={"role": new_role.value},
        ip_address=audit_context.ip_address if audit_context else None,
        request_id=audit_context.request_id if audit_context else None,
    )
    await db.commit()
    logger.info(
        "role changed user_id=%s %s->%s by=%s",
        user_id,
        old_role,
        new_role,
        actor.user_id,
    )
    return locked


async def batch_status_action(
    db: AsyncSession,
    actor: Actor,
    action: str,
    user_ids: list[UUID],
    *,
    audit_context: AuditContext | None = None,
) -> list[dict[str, object]]:
    """The §3.5 batch: ONE status action over up to 100 ids, one row at
    a time — each row its own audited transition, failures answered
    per-row ({ok: false, reason: <error code>}) without breaking the
    neighbours. The actor's own row is refused (a self-suspend would
    lock the console's operator out mid-flight)."""
    from app.modules.identity.account_admin_service import AccountAdminService

    service = AccountAdminService()
    method = "suspend_user" if action == "suspend" else "reactivate_user"
    results: list[dict[str, object]] = []
    for row_id in user_ids:
        if row_id == actor.user_id:
            results.append(
                {"user_id": str(row_id), "ok": False, "reason": "PERMISSION_DENIED"}
            )
            continue
        try:
            await getattr(service, method)(
                db, actor, row_id, reason="batch", audit_context=audit_context
            )
        except BusinessError as exc:
            results.append(
                {
                    "user_id": str(row_id),
                    "ok": False,
                    "reason": getattr(exc.code, "value", exc.code),
                }
            )
            continue
        results.append({"user_id": str(row_id), "ok": True})
    return results


async def overview(db: AsyncSession, *, clock: Clock) -> dict[str, Any]:
    """The §4.1 console aggregate: fixed key sets (zero counts carry),
    governance facts only — counts, never identities."""
    from datetime import time as _time

    def _fixed(pairs: dict[str, int], keys: tuple[str, ...]) -> dict[str, int]:
        return {key: pairs.get(key, 0) for key in keys}

    roles: dict[str, int] = {
        str(role): int(count)
        for role, count in await db.execute(
            select(User.role, func.count()).group_by(User.role)
        )
    }
    statuses: dict[str, int] = {
        str(status): int(count)
        for status, count in await db.execute(
            select(User.status, func.count()).group_by(User.status)
        )
    }
    total = int(await db.scalar(select(func.count()).select_from(User)) or 0)

    task_rows: dict[str, int] = {
        str(status): int(count)
        for status, count in await db.execute(
            select(_TASKS.c.status, func.count()).group_by(_TASKS.c.status)
        )
    }
    task_keys = (
        "DRAFT",
        "PUBLISHED",
        "PAUSED",
        "COMPLETED",
        "ARCHIVED",
        "DEPLETED",
    )

    pending_submissions = int(
        await db.scalar(
            select(func.count())
            .select_from(_SUBMISSIONS)
            .where(_SUBMISSIONS.c.review_status == "PENDING_REVIEW")
        )
        or 0
    )
    pending_redemptions = int(
        await db.scalar(
            select(func.count())
            .select_from(_REWARD_REDEMPTIONS)
            .where(_REWARD_REDEMPTIONS.c.status.in_(("REQUESTED", "UNDER_REVIEW")))
        )
        or 0
    )

    day_start = datetime.combine(
        clock.now().date(), _time.min, tzinfo=clock.now().tzinfo
    )
    new_users = int(
        await db.scalar(
            select(func.count()).select_from(User).where(User.created_at >= day_start)
        )
        or 0
    )
    active_users = int(
        await db.scalar(
            select(func.count(func.distinct(UserSession.user_id)))
            .select_from(UserSession)
            .where(
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > clock.now(),
            )
        )
        or 0
    )
    return {
        "users": {
            "total": total,
            "by_role": _fixed(roles, ("STUDENT", "TEACHER", "ADMIN")),
            "by_status": _fixed(
                statuses, ("ACTIVE", "PENDING_APPROVAL", "SUSPENDED", "BANNED")
            ),
        },
        "tasks": {"by_status": _fixed(task_rows, task_keys)},
        "pending_submissions": pending_submissions,
        "pending_redemptions": pending_redemptions,
        "today": {"new_users": new_users, "active_users": active_users},
    }
