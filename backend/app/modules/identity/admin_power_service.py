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
from typing import TypedDict
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity import device_credentials
from app.modules.identity.account_admin_service import (
    AccountAdminTargetNotFoundError as AdminTargetNotFoundError,
)
from app.modules.identity.events import Actor
from app.modules.identity.models import RememberedDevice, User, UserSession
from app.modules.identity.session_service import SessionService
from app.modules.points.models import PointWallet
from app.modules.submissions.models import Submission

logger = logging.getLogger(__name__)

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
