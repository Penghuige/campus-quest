# backend/app/modules/identity/admin_power_service.py
"""The admin console's cross-module read and emergency-stop use cases
(V1.1 Wave 2a, spec §3.1/§3.3).

``user_panorama`` is the §3.1 detail view: the account's own row plus
the wallet projection, the ten most recent submissions, and the live
credential counts (sessions, remembered devices). Read-only, one query
per fact — the console shape, not a domain aggregate.

``kick`` is the §3.3 stolen-account emergency stop: every still-live
refresh session revoked and every remembered-device credential dropped
in ONE transaction, with the ``ADMIN_SESSIONS_REVOKED`` audit row
riding the same commit (flush-only writer, the #80 discipline). The
revoked session rows survive (revocation is audit trail, never
deletion); device credentials go because their whole value is being a
live credential (the #87 module's design).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, TypedDict
from uuid import UUID

from sqlalchemy import func, select
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
from app.modules.points.models import PointWallet
from app.modules.submissions.models import Submission

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


class AdminPanorama(TypedDict):
    """The §3.1 facts, typed for the route's DTO assembly."""

    user: User
    wallet_balance: int
    recent_submissions: list[tuple[Submission, str]]
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

    wallet = await db.get(PointWallet, user_id)
    # Submissions hang off claims (claim -> student, claim -> task):
    # the strip joins through to the task title in one query.
    from app.modules.tasks.models import AssignmentClaim, Task

    recent = [
        (row[0], row[1])
        for row in (
            await db.execute(
                select(Submission, Task.title)
                .join(AssignmentClaim, Submission.claim_id == AssignmentClaim.id)
                .join(Task, AssignmentClaim.task_id == Task.id)
                .where(AssignmentClaim.user_id == user_id)
                .order_by(Submission.submitted_at.desc(), Submission.id)
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
        "wallet_balance": wallet.available_points if wallet else 0,
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
