"""Owner-only profile with transactional PII access audit, no qualification.

The users Core lock seam verifies only id/role/status, never identity ORM
or contact fields. It serializes changes against account suspension.
"""

from sqlalchemy import String, Uuid, column, select, table, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.innovation.models import OwnerProfile
from app.modules.innovation.owner_schemas import (
    OwnerProfileReadResponse,
    OwnerProfileResponse,
    OwnerProfileSave,
)

_USERS = table(
    "users", column("id", Uuid), column("role", String), column("status", String)
)


async def _require_student(db: AsyncSession, actor: Actor) -> None:
    account = (
        await db.execute(
            select(_USERS.c.role, _USERS.c.status)
            .where(_USERS.c.id == actor.user_id)
            .with_for_update()
        )
    ).one_or_none()
    if account is None:
        raise BusinessError(
            ErrorCode.AUTHENTICATION_REQUIRED, "请重新登录", status_code=401
        )
    if actor.role != Role.STUDENT or account.role != Role.STUDENT:
        raise BusinessError(
            ErrorCode.PERMISSION_DENIED, "仅学生可管理本人负责人资料", status_code=403
        )
    if account.status != UserStatus.ACTIVE:
        raise BusinessError(
            ErrorCode.ACCOUNT_NOT_ACTIVE, "账号当前不可用", status_code=403
        )


def _response(row: OwnerProfile) -> OwnerProfileResponse:
    return OwnerProfileResponse(
        name=row.name,
        student_no=row.student_no,
        major=row.major,
        grade=row.grade,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class OwnerProfileService:
    def __init__(self, *, clock: Clock, audit: AuditLogWriter | None = None) -> None:
        self._clock = clock
        self._audit = audit if audit is not None else AuditLogWriter()

    async def get_owned(
        self, db: AsyncSession, *, actor: Actor, context: AuditContext | None = None
    ) -> OwnerProfileReadResponse:
        await _require_student(db, actor)
        row = await db.scalar(
            select(OwnerProfile)
            .where(OwnerProfile.user_id == actor.user_id)
            .execution_options(populate_existing=True)
        )
        # Even self-service PII access is durable and fails closed on audit errors.
        await self._record(
            db, actor=actor, action="IE_OWNER_PROFILE_READ", context=context
        )
        result = OwnerProfileReadResponse(
            profile=_response(row) if row is not None else None
        )
        await db.commit()
        return result

    async def save(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        payload: OwnerProfileSave,
        context: AuditContext | None = None,
    ) -> OwnerProfileResponse:
        await _require_student(db, actor)
        values = payload.model_dump(exclude={"version"})
        now = self._clock.now()
        if payload.version == 0:
            creation = (
                pg_insert(OwnerProfile)
                .values(
                    user_id=actor.user_id,
                    **values,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=[OwnerProfile.user_id])
                .returning(OwnerProfile)
            )
            row = await db.scalar(creation.execution_options(populate_existing=True))
        else:
            statement = (
                update(OwnerProfile)
                .where(
                    OwnerProfile.user_id == actor.user_id,
                    OwnerProfile.version == payload.version,
                )
                .values(**values, version=OwnerProfile.version + 1, updated_at=now)
                .returning(OwnerProfile)
            )
            row = await db.scalar(statement.execution_options(populate_existing=True))
        if row is None:
            raise BusinessError(
                ErrorCode.OWNER_PROFILE_VERSION_CONFLICT,
                "资料版本已改变，请读取最新资料；当前输入尚未保存",
                status_code=409,
            )
        await self._record(
            db,
            actor=actor,
            action="IE_OWNER_PROFILE_SAVE",
            context=context,
            version=row.version,
        )
        result = _response(row)
        await db.commit()
        return result

    async def _record(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        action: str,
        context: AuditContext | None,
        version: int | None = None,
    ) -> None:
        await self._audit.append(
            db,
            actor=actor,
            action=action,
            target_type="ie_owner_profile",
            target_id=str(actor.user_id),
            reason="本人管理负责人资料",
            after_snapshot={"version": version} if version is not None else None,
            request_id=context.request_id if context else None,
            ip_address=context.ip_address if context else None,
        )
