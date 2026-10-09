"""Owner application snapshots and admin activation with real transactional audit."""

from uuid import UUID

from sqlalchemy import DateTime, String, Uuid, column, func, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.innovation.models import OwnerProfile, OwnerQualification
from app.modules.innovation.owner_service import _require_student
from app.modules.innovation.qualification_schemas import (
    QualificationApply,
    QualificationApprove,
    QualificationDetail,
    QualificationProfile,
    QualificationQueue,
    QualificationQueueItem,
    QualificationState,
)

_USERS = table(
    "users", column("id", Uuid), column("role", String), column("status", String)
)
_TOTP = table(
    "totp_credentials",
    column("user_id", Uuid),
    column("confirmed_at", DateTime(timezone=True)),
)


def _state(row: OwnerQualification | None) -> QualificationState:
    return QualificationState(
        status=row.status if row else "NOT_APPLIED",
        version=row.version if row else 0,
        profile_version=row.profile_version if row else None,
        requested_at=row.requested_at if row else None,
        approved_at=row.approved_at if row else None,
    )


def _conflict(message: str) -> BusinessError:
    return BusinessError(ErrorCode.CONFLICT, message, status_code=409)


class OwnerQualificationService:
    def __init__(self, *, clock: Clock, audit: AuditLogWriter | None = None) -> None:
        self._clock = clock
        self._audit = audit if audit is not None else AuditLogWriter()

    async def _admin(
        self, db: AsyncSession, actor: Actor, user_id: UUID | None = None
    ) -> None:
        # Same users->TOTP order as grants; serialize target suspension.
        rows = (
            await db.execute(
                select(_USERS)
                .where(
                    _USERS.c.id.in_(
                        {actor.user_id, user_id} if user_id else {actor.user_id}
                    )
                )
                .order_by(_USERS.c.id)
                .with_for_update()
            )
        ).all()
        accounts = {row.id: row for row in rows}
        admin = accounts.get(actor.user_id)
        if actor.role != Role.ADMIN or admin is None or admin.role != Role.ADMIN:
            raise BusinessError(
                ErrorCode.PERMISSION_DENIED,
                "仅管理员可处理负责人资格申请",
                status_code=403,
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
        if user_id is not None:
            target = accounts.get(user_id)
            if target is None:
                raise BusinessError(ErrorCode.NOT_FOUND, "申请不存在", status_code=404)
            if target.role != Role.STUDENT or target.status != UserStatus.ACTIVE:
                raise BusinessError(
                    ErrorCode.PERMISSION_DENIED,
                    "仅可处理正常学生的负责人资格申请",
                    status_code=403,
                )

    async def read_owned(self, db: AsyncSession, *, actor: Actor) -> QualificationState:
        await _require_student(db, actor)
        row = await db.scalar(
            select(OwnerQualification)
            .where(OwnerQualification.user_id == actor.user_id)
            .execution_options(populate_existing=True)
        )
        result = _state(row)
        await db.commit()
        return result

    async def apply(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        payload: QualificationApply,
        context: AuditContext | None = None,
    ) -> QualificationState:
        await _require_student(db, actor)
        profile = await db.scalar(
            select(OwnerProfile)
            .where(OwnerProfile.user_id == actor.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if profile is None or profile.version != payload.profile_version:
            raise _conflict("请先保存并读取最新的四项资料，再申请负责人资格")
        row = await db.scalar(
            select(OwnerQualification)
            .where(OwnerQualification.user_id == actor.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if payload.version != (row.version if row else 0) or (
            row is not None
            and (row.status == "APPROVED" or row.profile_version == profile.version)
        ):
            raise _conflict("资格或申请已改变，请重新读取后再决定")
        before: dict[str, str | int] | None = (
            {"status": row.status, "version": row.version} if row else None
        )
        if row is None:
            row = OwnerQualification(user_id=actor.user_id, status="PENDING", version=1)
            db.add(row)
        else:
            row.version += 1
        row.profile_version = profile.version
        row.name, row.student_no, row.major, row.grade = (
            profile.name,
            profile.student_no,
            profile.major,
            profile.grade,
        )
        row.requested_at = self._clock.now()
        await db.flush()
        await self._record(
            db,
            actor=actor,
            user_id=actor.user_id,
            action="IE_OWNER_QUALIFICATION_APPLY",
            reason="本人提交负责人资格申请",
            context=context,
            before=before,
            after={
                "status": row.status,
                "version": row.version,
                "profile_version": row.profile_version,
            },
        )
        result = _state(row)
        await db.commit()
        return result

    async def queue(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        limit: int,
        offset: int,
        context: AuditContext | None = None,
    ) -> QualificationQueue:
        await self._admin(db, actor)
        eligible = (
            select(OwnerQualification)
            .join(_USERS, _USERS.c.id == OwnerQualification.user_id)
            .where(
                OwnerQualification.status == "PENDING",
                _USERS.c.role == Role.STUDENT,
                _USERS.c.status == UserStatus.ACTIVE,
            )
        )
        total = await db.scalar(select(func.count()).select_from(eligible.subquery()))
        rows = list(
            await db.scalars(
                eligible.order_by(
                    OwnerQualification.requested_at, OwnerQualification.user_id
                )
                .limit(limit)
                .offset(offset)
            )
        )
        result = QualificationQueue(
            items=[
                QualificationQueueItem(
                    user_id=row.user_id,
                    version=row.version,
                    requested_at=row.requested_at,
                )
                for row in rows
            ],
            total=total or 0,
        )
        await self._record(
            db,
            actor=actor,
            user_id=actor.user_id,
            action="IE_OWNER_QUALIFICATION_QUEUE",
            reason="管理员查看待开通申请目录",
            context=context,
        )
        await db.commit()
        return result

    async def reveal(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        user_id: UUID,
        context: AuditContext | None = None,
    ) -> QualificationDetail:
        await self._admin(db, actor, user_id)
        row = await db.scalar(
            select(OwnerQualification)
            .where(OwnerQualification.user_id == user_id)
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise BusinessError(ErrorCode.NOT_FOUND, "申请不存在", status_code=404)
        result = QualificationDetail(
            **_state(row).model_dump(),
            user_id=user_id,
            profile=QualificationProfile(
                name=row.name,
                student_no=row.student_no,
                major=row.major,
                grade=row.grade,
            ),
        )
        await self._record(
            db,
            actor=actor,
            user_id=user_id,
            action="IE_OWNER_QUALIFICATION_REVEAL",
            reason="管理员查看已提交的四项申请快照",
            context=context,
            details={"version": row.version, "profile_version": row.profile_version},
        )
        # Durable audit must commit before PII can leave the service.
        await db.commit()
        return result

    async def approve(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        user_id: UUID,
        payload: QualificationApprove,
        context: AuditContext | None = None,
    ) -> QualificationState:
        await self._admin(db, actor, user_id)
        row = await db.scalar(
            select(OwnerQualification)
            .where(OwnerQualification.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise BusinessError(ErrorCode.NOT_FOUND, "申请不存在", status_code=404)
        if row.status != "PENDING" or row.version != payload.version:
            raise _conflict("申请状态已改变，请重新查看申请后再决定")
        before: dict[str, str | int] = {"status": row.status, "version": row.version}
        row.status = "APPROVED"
        row.version += 1
        row.approved_by = actor.user_id
        row.approved_at = self._clock.now()
        await db.flush()
        await self._record(
            db,
            actor=actor,
            user_id=user_id,
            action="IE_OWNER_QUALIFICATION_APPROVE",
            reason="管理员确认四项申请资料并开通资格",
            context=context,
            before=before,
            after={"status": row.status, "version": row.version},
        )
        result = _state(row)
        await db.commit()
        return result

    async def _record(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        user_id: UUID,
        action: str,
        reason: str,
        context: AuditContext | None,
        before: dict[str, str | int] | None = None,
        after: dict[str, str | int] | None = None,
        details: dict[str, str | int] | None = None,
    ) -> None:
        await self._audit.append(
            db,
            actor=actor,
            action=action,
            target_type="ie_owner_qualification",
            target_id=str(user_id),
            reason=reason,
            details=details,
            before_snapshot=before,
            after_snapshot=after,
            request_id=context.request_id if context else None,
            ip_address=context.ip_address if context else None,
        )
