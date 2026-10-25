"""Admin-designated student authority, never a global role promotion.

Minimal Core verification seams read/lock identity state; no identity ORM
or sensitive profile data crosses the module boundary. Consumer actions
must additionally enforce project scope and reviewer conflict exclusions.
"""

from uuid import UUID

from sqlalchemy import DateTime, String, Uuid, column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.innovation.models import OperationsGrant
from app.modules.innovation.operations_schemas import (
    InnovationCapabilitiesResponse,
    OperationsGrantResponse,
    OperationsGrantSave,
)
from app.modules.innovation.owner_service import _require_student

_USERS = table(
    "users", column("id", Uuid), column("role", String), column("status", String)
)
_TOTP = table(
    "totp_credentials",
    column("user_id", Uuid),
    column("confirmed_at", DateTime(timezone=True)),
)


class OperationsGrantService:
    def __init__(self, *, clock: Clock, audit: AuditLogWriter | None = None) -> None:
        self._clock = clock
        self._audit = audit if audit is not None else AuditLogWriter()

    async def _admin_target(
        self, db: AsyncSession, *, actor: Actor, user_id: UUID
    ) -> tuple[str, str]:
        # Deterministic lock order, also serializes target suspension against grant.
        rows = (
            await db.execute(
                select(_USERS)
                .where(_USERS.c.id.in_({actor.user_id, user_id}))
                .order_by(_USERS.c.id)
                .with_for_update()
            )
        ).all()
        accounts = {row.id: row for row in rows}
        admin = accounts.get(actor.user_id)
        if actor.role != Role.ADMIN or admin is None or admin.role != Role.ADMIN:
            raise BusinessError(
                ErrorCode.PERMISSION_DENIED, "仅管理员可以指定双创运营", status_code=403
            )
        if admin.status != UserStatus.ACTIVE:
            raise BusinessError(
                ErrorCode.ACCOUNT_NOT_ACTIVE, "账号当前不可用", status_code=403
            )
        confirmed = await db.scalar(
            select(_TOTP.c.confirmed_at)
            .where(_TOTP.c.user_id == actor.user_id)
            .with_for_update()
        )
        if confirmed is None:
            raise BusinessError(
                ErrorCode.TOTP_SETUP_REQUIRED,
                "请先完成管理员双重验证设置",
                status_code=403,
            )
        target = accounts.get(user_id)
        if target is None:
            raise BusinessError(ErrorCode.NOT_FOUND, "账号不存在", status_code=404)
        return target.role, target.status

    async def read(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        user_id: UUID,
        context: AuditContext | None = None,
    ) -> OperationsGrantResponse:
        await self._admin_target(db, actor=actor, user_id=user_id)
        row = await db.scalar(
            select(OperationsGrant)
            .where(OperationsGrant.user_id == user_id)
            .execution_options(populate_existing=True)
        )
        result = OperationsGrantResponse(
            user_id=user_id,
            enabled=row.enabled if row else False,
            version=row.version if row else 0,
        )
        await self._record(
            db,
            actor=actor,
            user_id=user_id,
            action="IE_OPERATIONS_GRANT_READ",
            context=context,
        )
        await db.commit()
        return result

    async def save(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        user_id: UUID,
        payload: OperationsGrantSave,
        context: AuditContext | None = None,
    ) -> OperationsGrantResponse:
        role, status = await self._admin_target(db, actor=actor, user_id=user_id)
        if payload.enabled and (role != Role.STUDENT or status != UserStatus.ACTIVE):
            raise BusinessError(
                ErrorCode.PERMISSION_DENIED,
                "仅可授权正常状态的学生账号",
                status_code=403,
            )
        row = await db.scalar(
            select(OperationsGrant)
            .where(OperationsGrant.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        previous = {
            "enabled": row.enabled if row else False,
            "version": row.version if row else 0,
        }
        if (
            payload.version != previous["version"]
            or payload.enabled == previous["enabled"]
        ):
            raise BusinessError(
                ErrorCode.CONFLICT,
                "授权状态已改变或操作重复，请重新读取后再决定",
                status_code=409,
            )
        if row is None:
            row = OperationsGrant(
                user_id=user_id,
                enabled=payload.enabled,
                version=1,
                changed_by=actor.user_id,
                updated_at=self._clock.now(),
            )
            db.add(row)
        else:
            row.enabled = payload.enabled
            row.version += 1
            row.changed_by = actor.user_id
            row.updated_at = self._clock.now()
        await db.flush()
        result = OperationsGrantResponse(
            user_id=user_id, enabled=row.enabled, version=row.version
        )
        await self._record(
            db,
            actor=actor,
            user_id=user_id,
            action="IE_OPERATIONS_GRANT_CHANGED",
            context=context,
            reason=payload.reason,
            before=previous,
            after={"enabled": row.enabled, "version": row.version},
        )
        await db.commit()
        return result

    async def capabilities(
        self, db: AsyncSession, *, actor: Actor
    ) -> InnovationCapabilitiesResponse:
        await _require_student(db, actor)
        enabled = await db.scalar(
            select(OperationsGrant.enabled).where(
                OperationsGrant.user_id == actor.user_id
            )
        )
        result = InnovationCapabilitiesResponse(operations_enabled=enabled is True)
        await db.commit()
        return result

    async def _record(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        user_id: UUID,
        action: str,
        context: AuditContext | None,
        reason: str = "管理员读取双创运营授权",
        before: dict[str, bool | int] | None = None,
        after: dict[str, bool | int] | None = None,
    ) -> None:
        await self._audit.append(
            db,
            actor=actor,
            action=action,
            target_type="ie_operations_grant",
            target_id=str(user_id),
            reason=reason,
            before_snapshot=before,
            after_snapshot=after,
            request_id=context.request_id if context else None,
            ip_address=context.ip_address if context else None,
        )
