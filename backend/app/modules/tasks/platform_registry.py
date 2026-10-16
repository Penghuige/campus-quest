# backend/app/modules/tasks/platform_registry.py
"""The platform registry's seed data and query seam (V1.1 spec 2026-10-16).

The seven shipped platforms are the migration's bulk-insert source AND
the constant the tests pin, so the DB and the code cannot drift apart.
Admin-created platforms live ONLY as rows — that is the entire point of
the registry (zero-migration growth).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.events import Actor
from app.modules.tasks.models import Platform

#: The shipped platforms (spec §1). Sort order groups the recruitment
#: cluster after the original three; display names are what the student
#: UI renders.
SEED_PLATFORMS: tuple[dict[str, Any], ...] = (
    {"code": "xiaohongshu", "display_name": "小红书", "sort_order": 10},
    {"code": "douyin", "display_name": "抖音", "sort_order": 20},
    {"code": "zhihu", "display_name": "知乎", "sort_order": 30},
    {"code": "boss-zhipin", "display_name": "BOSS直聘", "sort_order": 40},
    {"code": "zhaopin", "display_name": "智联招聘", "sort_order": 50},
    {"code": "58city", "display_name": "58同城", "sort_order": 60},
    {"code": "lietou", "display_name": "猎聘", "sort_order": 70},
)


async def enabled_codes(db: AsyncSession) -> set[str]:
    """Codes currently usable for NEW tasks/imports (spec §1 enabled)."""
    return set(
        await db.scalars(select(Platform.code).where(Platform.enabled.is_(True)))
    )


AUDIT_PLATFORM_CREATED = "PLATFORM_CREATED"
AUDIT_PLATFORM_UPDATED = "PLATFORM_UPDATED"
_PLATFORM_NOT_FOUND = "平台不存在"
_PLATFORM_CONFLICT = "该平台代码已存在"
_PLATFORM_INVALID_MESSAGE = "平台代码或显示名不能为空"


async def list_enabled(db: AsyncSession) -> list[Platform]:
    """The public dictionary: enabled only, sort_order ascending with
    code as the stable tiebreaker (spec §3)."""
    return list(
        await db.scalars(
            select(Platform)
            .where(Platform.enabled.is_(True))
            .order_by(Platform.sort_order, Platform.code)
        )
    )


async def list_all(db: AsyncSession) -> list[Platform]:
    """The admin console list: every row, same ordering."""
    return list(
        await db.scalars(select(Platform).order_by(Platform.sort_order, Platform.code))
    )


async def create_platform(
    db: AsyncSession,
    *,
    actor: Actor,
    code: str,
    display_name: str,
    sort_order: int,
    audit_context: AuditContext | None = None,
    audit: AuditLogWriter | None = None,
) -> Platform:
    """Register one platform (admin self-service, zero-migration growth).

    The row and its PLATFORM_CREATED audit line commit as one unit
    (flush-only writer, the PR #80 discipline). A duplicate code is the
    typed 409.
    """
    writer = audit if audit is not None else AuditLogWriter()
    normalized = code.strip().lower()
    if not normalized or not display_name.strip():
        # The DTO band already stops most of this; the service re-checks
        # so a blank-after-strip value can never become a dirty row (the
        # friendly-before-constraint rule, engineering §6).
        raise BusinessError(
            ErrorCode.VALIDATION_ERROR, _PLATFORM_INVALID_MESSAGE, status_code=400
        )
    if await db.get(Platform, normalized) is not None:
        raise BusinessError(ErrorCode.CONFLICT, _PLATFORM_CONFLICT, status_code=409)
    platform = Platform(
        code=normalized, display_name=display_name.strip(), sort_order=sort_order
    )
    db.add(platform)
    try:
        await db.flush()
    except IntegrityError as exc:
        # Two concurrent creates of the same code both pass the read
        # check; the loser's PK violation is the typed 409 (the
        # community rating_service race precedent), never a raw 500.
        await db.rollback()
        raise BusinessError(
            ErrorCode.CONFLICT, _PLATFORM_CONFLICT, status_code=409
        ) from exc
    await writer.append(
        db,
        actor=actor,
        action=AUDIT_PLATFORM_CREATED,
        target_type="platform",
        target_id=normalized,
        after_snapshot={
            "display_name": platform.display_name,
            "sort_order": platform.sort_order,
            "enabled": True,
        },
        ip_address=audit_context.ip_address if audit_context else None,
        request_id=audit_context.request_id if audit_context else None,
    )
    await db.commit()
    return platform


async def update_platform(
    db: AsyncSession,
    *,
    actor: Actor,
    code: str,
    display_name: str | None = None,
    sort_order: int | None = None,
    enabled: bool | None = None,
    audit_context: AuditContext | None = None,
    audit: AuditLogWriter | None = None,
) -> Platform:
    """Patch the admin-managed knobs; code is immutable by signature.

    The row's change and its PLATFORM_UPDATED audit line (before/after
    snapshots) commit as one unit. An unknown code is the typed 404.
    """
    writer = audit if audit is not None else AuditLogWriter()
    platform = await db.get(Platform, code)
    if platform is None:
        raise BusinessError(ErrorCode.NOT_FOUND, _PLATFORM_NOT_FOUND, status_code=404)
    before = {
        "display_name": platform.display_name,
        "sort_order": platform.sort_order,
        "enabled": platform.enabled,
    }
    if display_name is not None:
        if not display_name.strip():
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _PLATFORM_INVALID_MESSAGE,
                status_code=400,
            )
        platform.display_name = display_name.strip()
    if sort_order is not None:
        platform.sort_order = sort_order
    if enabled is not None:
        platform.enabled = enabled
    await db.flush()
    after = {
        "display_name": platform.display_name,
        "sort_order": platform.sort_order,
        "enabled": platform.enabled,
    }
    await writer.append(
        db,
        actor=actor,
        action=AUDIT_PLATFORM_UPDATED,
        target_type="platform",
        target_id=platform.code,
        before_snapshot=before,
        after_snapshot=after,
        ip_address=audit_context.ip_address if audit_context else None,
        request_id=audit_context.request_id if audit_context else None,
    )
    await db.commit()
    return platform
