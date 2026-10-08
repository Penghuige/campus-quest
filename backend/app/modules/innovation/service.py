"""Owner-scoped PostgreSQL operations, with service-owned commits.

The current user row is checked again inside the service; writes lock that
row so account suspension and draft changes serialize. Creation has a DB
unique key and immutable fingerprint, while edits use an atomic version CAS.
Nothing here grants project-owner qualification or publishes any content.
"""

import hashlib
import json
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.innovation.models import ProjectDraft
from app.modules.innovation.schemas import (
    ProjectDraftCreate,
    ProjectDraftListResponse,
    ProjectDraftResponse,
    ProjectDraftUpdate,
)

_CONTENT_FIELDS = ("title", "summary", "direction", "stage", "team_status")


def _response(row: ProjectDraft) -> ProjectDraftResponse:
    return ProjectDraftResponse(
        id=row.id,
        title=row.title,
        summary=row.summary,
        direction=row.direction,
        stage=row.stage,
        team_status=row.team_status,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _not_found() -> BusinessError:
    return BusinessError(ErrorCode.NOT_FOUND, "项目草稿不存在", status_code=404)


async def _require_student(
    db: AsyncSession, actor: Actor, *, lock: bool = False
) -> None:
    query = (
        select(User)
        .where(User.id == actor.user_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update()
    user = await db.scalar(query)
    if user is None:
        raise BusinessError(
            ErrorCode.AUTHENTICATION_REQUIRED, "请重新登录", status_code=401
        )
    if actor.role != Role.STUDENT or user.role != Role.STUDENT:
        raise BusinessError(
            ErrorCode.PERMISSION_DENIED, "仅学生可管理本人项目草稿", status_code=403
        )
    if user.status != UserStatus.ACTIVE:
        raise BusinessError(
            ErrorCode.ACCOUNT_NOT_ACTIVE, "账号当前不可用", status_code=403
        )


class ProjectDraftService:
    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock

    async def create(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        payload: ProjectDraftCreate,
    ) -> tuple[ProjectDraftResponse, bool]:
        await _require_student(db, actor, lock=True)
        content = payload.model_dump(include=set(_CONTENT_FIELDS))
        fingerprint = hashlib.sha256(
            json.dumps(
                content,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        now = self._clock.now()
        row = await db.scalar(
            pg_insert(ProjectDraft)
            .values(
                owner_user_id=actor.user_id,
                creation_request_id=payload.request_id,
                creation_payload_fingerprint=fingerprint,
                **content,
                version=1,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_ie_project_drafts_owner_request")
            .returning(ProjectDraft)
        )
        created = row is not None
        if row is None:
            row = await db.scalar(
                select(ProjectDraft)
                .where(
                    ProjectDraft.owner_user_id == actor.user_id,
                    ProjectDraft.creation_request_id == payload.request_id,
                )
                .execution_options(populate_existing=True)
            )
            if row is None:
                raise RuntimeError("Conflicting draft creation row disappeared")
            if row.creation_payload_fingerprint != fingerprint:
                raise BusinessError(
                    ErrorCode.PROJECT_DRAFT_REQUEST_CONFLICT,
                    "此创建请求已用于不同内容，请重新新建草稿",
                    status_code=409,
                )
        result = _response(row)
        await db.commit()
        return result, created

    async def list_owned(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        limit: int,
        offset: int,
    ) -> ProjectDraftListResponse:
        await _require_student(db, actor)
        owned = ProjectDraft.owner_user_id == actor.user_id
        total = await db.scalar(
            select(func.count()).select_from(ProjectDraft).where(owned)
        )
        rows = await db.scalars(
            select(ProjectDraft)
            .where(owned)
            .order_by(
                ProjectDraft.updated_at.desc(),
                ProjectDraft.id.desc(),
            )
            .limit(limit)
            .offset(offset)
            .execution_options(populate_existing=True)
        )
        return ProjectDraftListResponse(
            items=[_response(row) for row in rows],
            total=total or 0,
            limit=limit,
            offset=offset,
        )

    async def get_owned(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        draft_id: UUID,
    ) -> ProjectDraftResponse:
        await _require_student(db, actor)
        row = await db.scalar(
            select(ProjectDraft)
            .where(
                ProjectDraft.id == draft_id,
                ProjectDraft.owner_user_id == actor.user_id,
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise _not_found()
        return _response(row)

    async def update(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        draft_id: UUID,
        payload: ProjectDraftUpdate,
    ) -> ProjectDraftResponse:
        await _require_student(db, actor, lock=True)
        owned = (
            ProjectDraft.id == draft_id,
            ProjectDraft.owner_user_id == actor.user_id,
        )
        content = payload.model_dump(include=set(_CONTENT_FIELDS))
        row = await db.scalar(
            update(ProjectDraft)
            .where(
                *owned,
                ProjectDraft.version == payload.version,
            )
            .values(
                **content,
                version=ProjectDraft.version + 1,
                updated_at=self._clock.now(),
            )
            .returning(
                ProjectDraft,
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            if await db.scalar(select(ProjectDraft.id).where(*owned)) is None:
                raise _not_found()
            raise BusinessError(
                ErrorCode.PROJECT_DRAFT_VERSION_CONFLICT,
                "草稿已在其他页面更新，请刷新后重新保存；当前输入尚未保存",
                status_code=409,
            )
        result = _response(row)
        await db.commit()
        return result
