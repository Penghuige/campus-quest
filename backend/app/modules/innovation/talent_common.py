"""Talent transactions and minimal identity locks, without qualification."""

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.innovation.accounts import USERS


def fingerprint(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def conflict(message: str, *, kind: str = "VERSION_CONFLICT") -> BusinessError:
    return BusinessError(
        ErrorCode.CONFLICT, message, status_code=409, details={"kind": kind}
    )


def not_found() -> BusinessError:
    return BusinessError(
        ErrorCode.NOT_FOUND, "档案不存在或当前不可查看", status_code=404
    )


@asynccontextmanager
async def mutation(db: AsyncSession) -> AsyncIterator[None]:
    try:
        yield
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def lock_accounts(
    db: AsyncSession, *, actor: Actor, owner_id: UUID, student: bool
) -> None:
    rows = (
        await db.execute(
            select(USERS.c.id, USERS.c.role, USERS.c.status)
            .where(USERS.c.id.in_({actor.user_id, owner_id}))
            .order_by(USERS.c.id)
            .with_for_update()
        )
    ).all()
    accounts = {row.id: row for row in rows}
    current = accounts.get(actor.user_id)
    if (
        current is None
        or current.status != UserStatus.ACTIVE
        or current.role != actor.role
    ):
        raise BusinessError(
            ErrorCode.ACCOUNT_NOT_ACTIVE,
            "登录身份当前不可用，请重新登录",
            status_code=403,
        )
    owner = accounts.get(owner_id)
    if owner is None or owner.role != Role.STUDENT or owner.status != UserStatus.ACTIVE:
        raise not_found()
    if student and (actor.user_id != owner_id or current.role != Role.STUDENT):
        raise BusinessError(
            ErrorCode.PERMISSION_DENIED, "仅学生可维护本人档案", status_code=403
        )
