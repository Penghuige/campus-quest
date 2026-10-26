"""Private parent/child scope, immutable creation fingerprint and version CAS."""

import hashlib
import json
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.events import Actor
from app.modules.innovation.achievement_schemas import (
    AchievementDraftCreate,
    AchievementDraftListResponse,
    AchievementDraftResponse,
    AchievementDraftUpdate,
)
from app.modules.innovation.models import AchievementDraft, ProjectDraft
from app.modules.innovation.owner_service import _require_student
from app.modules.innovation.workflow_guards import require_editable

_FIELDS = ("title", "description", "work_url", "award_text")


def _response(row: AchievementDraft) -> AchievementDraftResponse:
    return AchievementDraftResponse(
        id=row.id,
        title=row.title,
        description=row.description,
        work_url=row.work_url,
        award_text=row.award_text,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _not_found() -> BusinessError:
    return BusinessError(ErrorCode.NOT_FOUND, "项目或成果草稿不存在", status_code=404)


class AchievementDraftService:
    def __init__(self, *, clock: Clock, audit: AuditLogWriter | None = None) -> None:
        self._clock = clock
        self._audit = audit if audit is not None else AuditLogWriter()

    async def _owned_parent(
        self, db: AsyncSession, *, actor: Actor, project_id: UUID
    ) -> None:
        await _require_student(db, actor)
        parent = await db.scalar(
            select(ProjectDraft.id)
            .where(
                ProjectDraft.id == project_id,
                ProjectDraft.owner_user_id == actor.user_id,
            )
            .with_for_update()
        )
        if parent is None:
            raise _not_found()

    async def create(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        payload: AchievementDraftCreate,
        context: AuditContext | None = None,
    ) -> tuple[AchievementDraftResponse, bool]:
        await self._owned_parent(db, actor=actor, project_id=project_id)
        content = payload.model_dump(include=set(_FIELDS))
        fingerprint = hashlib.sha256(
            json.dumps(
                content, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        now = self._clock.now()
        row = await db.scalar(
            pg_insert(AchievementDraft)
            .values(
                project_id=project_id,
                creation_request_id=payload.request_id,
                creation_payload_fingerprint=fingerprint,
                **content,
                version=1,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                constraint="uq_ie_achievement_drafts_project_request"
            )
            .returning(AchievementDraft)
            .execution_options(populate_existing=True)
        )
        created = row is not None
        if row is None:
            row = await db.scalar(
                select(AchievementDraft)
                .where(
                    AchievementDraft.project_id == project_id,
                    AchievementDraft.creation_request_id == payload.request_id,
                )
                .execution_options(populate_existing=True)
            )
            if row is None:
                raise RuntimeError("Conflicting achievement creation row disappeared")
            if row.creation_payload_fingerprint != fingerprint:
                raise BusinessError(
                    ErrorCode.CONFLICT,
                    "这次新建请求已用于其他内容，请从列表核对后继续",
                    status_code=409,
                    details={"kind": "achievement_request"},
                )
        result = _response(row)
        await self._record(
            db,
            actor=actor,
            target_id=row.id,
            action="IE_ACHIEVEMENT_DRAFT_CREATE"
            if created
            else "IE_ACHIEVEMENT_DRAFT_READ",
            version=row.version,
            context=context,
        )
        await db.commit()
        return result, created

    async def list_owned(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        limit: int,
        offset: int,
        context: AuditContext | None = None,
    ) -> AchievementDraftListResponse:
        await self._owned_parent(db, actor=actor, project_id=project_id)
        total = await db.scalar(
            select(func.count())
            .select_from(AchievementDraft)
            .where(AchievementDraft.project_id == project_id)
        )
        rows = await db.scalars(
            select(AchievementDraft)
            .where(AchievementDraft.project_id == project_id)
            .order_by(AchievementDraft.updated_at.desc(), AchievementDraft.id.desc())
            .limit(limit)
            .offset(offset)
            .execution_options(populate_existing=True)
        )
        result = AchievementDraftListResponse(
            items=[_response(row) for row in rows],
            total=total or 0,
            limit=limit,
            offset=offset,
        )
        await self._record(
            db,
            actor=actor,
            target_id=project_id,
            action="IE_ACHIEVEMENT_DRAFT_READ",
            context=context,
            target_type="ie_project_draft",
        )
        await db.commit()
        return result

    async def get_owned(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        achievement_id: UUID,
        context: AuditContext | None = None,
    ) -> AchievementDraftResponse:
        await self._owned_parent(db, actor=actor, project_id=project_id)
        row = await db.scalar(
            select(AchievementDraft)
            .where(
                AchievementDraft.id == achievement_id,
                AchievementDraft.project_id == project_id,
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise _not_found()
        result = _response(row)
        await self._record(
            db,
            actor=actor,
            target_id=row.id,
            action="IE_ACHIEVEMENT_DRAFT_READ",
            context=context,
        )
        await db.commit()
        return result

    async def update(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        achievement_id: UUID,
        payload: AchievementDraftUpdate,
        context: AuditContext | None = None,
    ) -> AchievementDraftResponse:
        await self._owned_parent(db, actor=actor, project_id=project_id)
        owned = (
            AchievementDraft.id == achievement_id,
            AchievementDraft.project_id == project_id,
        )
        existing = await db.scalar(select(AchievementDraft.id).where(*owned))
        if existing is None:
            raise _not_found()
        await require_editable(db, achievement_id)
        row = await db.scalar(
            update(AchievementDraft)
            .where(*owned, AchievementDraft.version == payload.version)
            .values(
                **payload.model_dump(include=set(_FIELDS)),
                version=AchievementDraft.version + 1,
                updated_at=self._clock.now(),
            )
            .returning(AchievementDraft)
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise BusinessError(
                ErrorCode.CONFLICT,
                "成果草稿已更新，请读取最新版本；当前输入尚未保存",
                status_code=409,
                details={"kind": "achievement_version"},
            )
        result = _response(row)
        await self._record(
            db,
            actor=actor,
            target_id=row.id,
            action="IE_ACHIEVEMENT_DRAFT_UPDATE",
            version=row.version,
            context=context,
        )
        await db.commit()
        return result

    async def _record(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        target_id: UUID,
        action: str,
        context: AuditContext | None,
        version: int | None = None,
        target_type: str = "ie_achievement_draft",
    ) -> None:
        await self._audit.append(
            db,
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            reason="本人准备成果草稿",
            after_snapshot={"version": version} if version is not None else None,
            request_id=context.request_id if context else None,
            ip_address=context.ip_address if context else None,
        )
