"""Whitelist campus projections and final, audited consent checks."""

import asyncio
import hashlib
from typing import Any
from uuid import UUID

from sqlalchemy import Select, Text, func, literal, or_, select
from sqlalchemy.dialects.postgresql import JSONPATH
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.integrations.errors import ProviderError
from app.integrations.evidence_scanner import MAX_EVIDENCE_BYTES
from app.integrations.object_storage import ObjectStorage, StoredObject
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.innovation.talent_common import USERS, conflict, mutation, not_found
from app.modules.innovation.talent_models import (
    TalentFile,
    TalentProfile,
    TalentRevision,
    TalentRevisionFile,
)
from app.modules.innovation.talent_schemas import (
    PublicTalentContent,
    PublicTalentFile,
    PublicTalentListResponse,
    PublicTalentResponse,
    PublicTalentSummary,
    TalentContactsResponse,
    TalentQuery,
)
from app.modules.innovation.talent_service import TalentService


def literal_pattern(value: str) -> str:
    return (
        "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    )


def readable_material_conditions() -> tuple[ColumnElement[bool], ...]:
    """One availability rule for the directory fact, detail and download."""
    return (
        TalentFile.state == "READY",
        TalentFile.removed_at.is_(None),
        TalentFile.revoked_at.is_(None),
    )


def has_public_materials() -> ColumnElement[bool]:
    # Correlated EXISTS preserves one row per profile and uses the revision/file
    # primary keys; no extra query per result or scan of private upload inventory.
    return (
        select(TalentRevisionFile.file_id)
        .join(TalentFile, TalentFile.id == TalentRevisionFile.file_id)
        .where(
            TalentRevisionFile.revision_id == TalentRevision.id,
            TalentFile.profile_id == TalentProfile.id,
            *readable_material_conditions(),
        )
        .correlate(TalentRevision, TalentProfile)
        .exists()
    )


class PublicTalentService:
    def __init__(
        self,
        *,
        clock: Clock,
        storage: ObjectStorage,
        audit: AuditLogWriter | None = None,
    ) -> None:
        self.clock, self.storage = clock, storage
        self.talents = TalentService(clock=clock, audit=audit)

    def query(self, actor: Actor) -> Select[Any]:
        viewer = USERS.alias("talent_viewer")
        return (
            select(
                TalentProfile.id,
                TalentProfile.public_version,
                TalentProfile.public_updated_at.label("published_at"),
                TalentRevision.id.label("revision_id"),
                TalentRevision.content,
                has_public_materials().label("has_public_materials"),
                func.jsonb_path_query_array(
                    TalentRevision.contacts, literal("$[*].channel").cast(JSONPATH)
                ).label("channels"),
            )
            .join(TalentRevision, TalentRevision.id == TalentProfile.public_revision_id)
            .join(USERS, USERS.c.id == TalentProfile.owner_user_id)
            .where(
                TalentProfile.visibility == "VISIBLE",
                TalentProfile.moderation_state == "NORMAL",
                USERS.c.role == "STUDENT",
                USERS.c.status == "ACTIVE",
                select(viewer.c.id)
                .where(
                    viewer.c.id == actor.user_id,
                    viewer.c.role == actor.role,
                    # Reading surface mirrors require_active_actor: a
                    # PENDING_APPROVAL account browses (V1.1 2026-10-16
                    # §2); the acting guards stay strict.
                    viewer.c.status.in_(("ACTIVE", "PENDING_APPROVAL")),
                )
                .exists(),
            )
        )

    async def viewer_current(self, db: AsyncSession, actor: Actor) -> None:
        current = await db.scalar(
            select(USERS.c.id).where(
                USERS.c.id == actor.user_id,
                USERS.c.role == actor.role,
                # Same reading-surface rule as the visibility query above.
                USERS.c.status.in_(("ACTIVE", "PENDING_APPROVAL")),
            )
        )
        if current is None:
            raise BusinessError(
                ErrorCode.ACCOUNT_NOT_ACTIVE,
                "登录身份当前不可用，请重新登录",
                status_code=403,
            )

    async def list_visible(
        self, db: AsyncSession, *, actor: Actor, query: TalentQuery
    ) -> PublicTalentListResponse:
        await self.viewer_current(db, actor)
        statement = self.query(actor)
        content = TalentRevision.content
        if query.q:
            expressions = [
                content[name].astext
                for name in (
                    "name",
                    "faculty",
                    "introduction",
                    "advantages",
                    "skill_notes",
                    "cooperation_direction",
                    "desired_roles",
                    "experience",
                )
            ]
            expressions.append(
                func.jsonb_path_query_array(
                    content, literal("$.tags[*].name").cast(JSONPATH)
                ).cast(Text)
            )
            statement = statement.where(
                or_(
                    *(
                        value.ilike(literal_pattern(query.q), escape="\\")
                        for value in expressions
                    )
                )
            )
        for tag in query.tag_ids:
            statement = statement.where(content["tag_ids"].contains([str(tag)]))
        if query.cooperation_state:
            statement = statement.where(
                content["cooperation_state"].astext == query.cooperation_state
            )
        if query.has_public_materials is not None:
            statement = statement.where(
                has_public_materials() == query.has_public_materials
            )
        total = await db.scalar(select(func.count()).select_from(statement.subquery()))
        rows = (
            (
                await db.execute(
                    statement.order_by(
                        TalentProfile.public_updated_at.desc(), TalentProfile.id.asc()
                    )
                    .limit(query.limit)
                    .offset(query.offset)
                )
            )
            .mappings()
            .all()
        )
        items = [
            PublicTalentSummary.model_validate(
                {
                    "id": row["id"],
                    "public_version": row["public_version"],
                    "published_at": row["published_at"],
                    "has_public_materials": row["has_public_materials"],
                    **{
                        name: row["content"][name]
                        for name in (
                            "name",
                            "faculty",
                            "grade",
                            "tags",
                            "cooperation_state",
                        )
                    },
                    "introduction": row["content"]["introduction"][:200],
                    "advantages": row["content"]["advantages"][:200],
                }
            )
            for row in rows
        ]
        return PublicTalentListResponse(
            items=items, total=total or 0, limit=query.limit, offset=query.offset
        )

    async def get_visible(
        self, db: AsyncSession, *, actor: Actor, profile_id: UUID
    ) -> PublicTalentResponse:
        await self.viewer_current(db, actor)
        row = (
            (await db.execute(self.query(actor).where(TalentProfile.id == profile_id)))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise not_found()
        fields = PublicTalentContent.model_fields
        content = PublicTalentContent.model_validate(
            {name: row["content"][name] for name in fields}
        )
        files = (
            (
                await db.execute(
                    select(
                        TalentFile.id,
                        TalentFile.name,
                        TalentFile.content_type,
                        TalentFile.size,
                    )
                    .join(
                        TalentRevisionFile, TalentRevisionFile.file_id == TalentFile.id
                    )
                    .where(
                        TalentRevisionFile.revision_id == row["revision_id"],
                        TalentFile.profile_id == profile_id,
                        *readable_material_conditions(),
                    )
                    .order_by(TalentFile.created_at, TalentFile.id)
                )
            )
            .mappings()
            .all()
        )
        return PublicTalentResponse(
            id=profile_id,
            content=content,
            files=[PublicTalentFile.model_validate(dict(file)) for file in files],
            contact_channels=row["channels"],
            public_version=row["public_version"],
            published_at=row["published_at"],
        )

    async def lock_visible(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        profile_id: UUID,
        expected_public_version: int,
    ) -> TalentProfile:
        owner_id = await db.scalar(
            select(TalentProfile.owner_user_id).where(TalentProfile.id == profile_id)
        )
        if owner_id is None:
            raise not_found()
        row = await self.talents.lock_owned(
            db, actor=Actor(owner_id, Role.STUDENT), viewer=actor
        )
        if (
            row.id != profile_id
            or row.visibility != "VISIBLE"
            or row.moderation_state != "NORMAL"
            or row.public_revision_id is None
        ):
            raise not_found()
        if row.public_version != expected_public_version:
            raise conflict(
                "资料已更新，请刷新后重新确认联系或下载", kind="PUBLIC_VERSION_CHANGED"
            )
        return row

    async def get_contacts(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        profile_id: UUID,
        expected_public_version: int,
        context: AuditContext,
    ) -> TalentContactsResponse:
        async with mutation(db):
            row = await self.lock_visible(
                db,
                actor=actor,
                profile_id=profile_id,
                expected_public_version=expected_public_version,
            )
            contacts = await db.scalar(
                select(TalentRevision.contacts).where(
                    TalentRevision.id == row.public_revision_id
                )
            )
            await self.talents.record_audit(
                db, actor=actor, row=row, action="contacts-read", context=context
            )
            return TalentContactsResponse(
                public_version=row.public_version, contacts=contacts or []
            )

    async def lock_public_file(
        self, db: AsyncSession, row: TalentProfile, file_id: UUID
    ) -> TalentFile:
        file = await db.scalar(
            select(TalentFile)
            .where(
                TalentFile.id == file_id,
                TalentFile.profile_id == row.id,
                *readable_material_conditions(),
                select(TalentRevisionFile.file_id)
                .where(
                    TalentRevisionFile.revision_id == row.public_revision_id,
                    TalentRevisionFile.file_id == file_id,
                )
                .exists(),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if file is None:
            raise not_found()
        return file

    async def read_file_content(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        profile_id: UUID,
        file_id: UUID,
        expected_public_version: int,
        context: AuditContext,
    ) -> StoredObject:
        async with mutation(db):
            row = await self.lock_visible(
                db,
                actor=actor,
                profile_id=profile_id,
                expected_public_version=expected_public_version,
            )
            file = await self.lock_public_file(db, row, file_id)
            key, size, mime, digest = (
                file.object_key,
                file.size,
                file.content_type,
                file.sha256,
            )
        try:
            content = await asyncio.wait_for(
                asyncio.to_thread(
                    self.storage.read_bounded_object,
                    object_key=key,
                    max_bytes=MAX_EVIDENCE_BYTES,
                ),
                timeout=140,
            )
        except (ProviderError, OSError, ValueError, TimeoutError) as error:
            raise conflict(
                "作品暂不可读取，请稍后重试", kind="FILE_UNAVAILABLE"
            ) from error
        if (
            len(content.content) != size
            or content.content_type != mime
            or hashlib.sha256(content.content).hexdigest() != digest
        ):
            raise conflict("作品内容校验未通过，请联系发布者", kind="FILE_CHANGED")
        async with mutation(db):
            row = await self.lock_visible(
                db,
                actor=actor,
                profile_id=profile_id,
                expected_public_version=expected_public_version,
            )
            file = await self.lock_public_file(db, row, file_id)
            if file.sha256 != digest or file.object_key != key:
                raise conflict("作品内容已改变，请刷新", kind="FILE_CHANGED")
            await self.talents.record_audit(
                db, actor=actor, row=row, action="file-read", context=context
            )
        return content
