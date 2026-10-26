"""Whitelist queries and fresh, audited contact access with deterministic locks."""

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Row, Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.events import Actor
from app.modules.innovation.accounts import USERS as _USERS
from app.modules.innovation.contact_schemas import ContactChannel, ContactInput
from app.modules.innovation.models import OwnerQualification, ProjectDraft
from app.modules.innovation.public_achievement_service import PublicAchievementService
from app.modules.innovation.recruitment_models import (
    RecruitmentContactAuthorization,
    RecruitmentDraft,
    RecruitmentRevision,
)
from app.modules.innovation.recruitment_schemas import (
    PublicProjectFilters,
    PublicRecruitmentContent,
    PublicRecruitmentListResponse,
    PublicRecruitmentResponse,
    RecruitmentContactsResponse,
)
from app.modules.innovation.recruitment_service import mutation, not_found

PublicRecruitmentProjection = tuple[UUID, UUID, dict[str, Any], datetime, int, UUID]


def pattern(value: str) -> str:
    return (
        "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    )


class PublicRecruitmentService:
    def __init__(self, *, clock: Clock, audit: AuditLogWriter | None = None) -> None:
        self._clock = clock
        self._audit = audit or AuditLogWriter()

    def query(self) -> Select[PublicRecruitmentProjection]:
        return (
            select(
                RecruitmentDraft.id,
                RecruitmentDraft.project_id,
                RecruitmentRevision.content,
                RecruitmentRevision.created_at,
                RecruitmentRevision.number,
                RecruitmentRevision.id.label("revision_id"),
            )
            .join(
                RecruitmentRevision,
                RecruitmentDraft.public_revision_id == RecruitmentRevision.id,
            )
            .join(ProjectDraft, ProjectDraft.id == RecruitmentDraft.project_id)
            .join(_USERS, _USERS.c.id == ProjectDraft.owner_user_id)
            .join(OwnerQualification, OwnerQualification.user_id == _USERS.c.id)
            .where(
                RecruitmentDraft.status == "OPEN",
                RecruitmentDraft.moderation_state == "NORMAL",
                or_(
                    RecruitmentDraft.deadline_at.is_(None),
                    RecruitmentDraft.deadline_at > self._clock.now(),
                ),
                _USERS.c.role == "STUDENT",
                _USERS.c.status == "ACTIVE",
                OwnerQualification.status == "APPROVED",
                select(RecruitmentContactAuthorization.id)
                .where(
                    RecruitmentContactAuthorization.revision_id
                    == RecruitmentDraft.public_revision_id,
                    RecruitmentContactAuthorization.revoked_at.is_(None),
                )
                .exists(),
            )
        )

    def filtered(
        self, filters: PublicProjectFilters, *, project_id: UUID | None = None
    ) -> Select[PublicRecruitmentProjection]:
        query = self.query()
        content = RecruitmentRevision.content
        if project_id is not None:
            query = query.where(RecruitmentDraft.project_id == project_id)
        if filters.q:
            query = query.where(
                or_(
                    *(
                        content[name].astext.ilike(pattern(filters.q), escape="\\")
                        for name in (
                            "project_title",
                            "introduction",
                            "role_name",
                            "duties",
                            "requirements",
                        )
                    )
                )
            )
        for name in ("direction", "stage"):
            value = getattr(filters, name)
            if value:
                query = query.where(
                    content[name].astext.ilike(pattern(value), escape="\\")
                )
        if filters.beginner_friendly is not None:
            query = query.where(
                content["beginner_friendly"].as_boolean() == filters.beginner_friendly
            )
        return query

    async def responses(
        self, db: AsyncSession, rows: list[Row[PublicRecruitmentProjection]]
    ) -> list[PublicRecruitmentResponse]:
        if not rows:
            return []
        channels: dict[UUID, list[ContactChannel]] = {}
        # One contact-kind query for the entire page; values are never selected.
        for revision_id, channel in (
            await db.execute(
                select(
                    RecruitmentContactAuthorization.revision_id,
                    RecruitmentContactAuthorization.channel,
                )
                .where(
                    RecruitmentContactAuthorization.revision_id.in_(
                        [row.revision_id for row in rows]
                    ),
                    RecruitmentContactAuthorization.revoked_at.is_(None),
                )
                .order_by(RecruitmentContactAuthorization.channel)
            )
        ).all():
            channels.setdefault(revision_id, []).append(cast(ContactChannel, channel))
        return [
            PublicRecruitmentResponse(
                id=row.id,
                project_id=row.project_id,
                revision_number=row.number,
                content=PublicRecruitmentContent.model_validate(row.content),
                contact_channels=channels.get(row.revision_id, []),
                published_at=row.created_at,
            )
            for row in rows
        ]

    async def page(
        self,
        db: AsyncSession,
        *,
        filters: PublicProjectFilters,
        limit: int,
        offset: int,
        project_id: UUID | None = None,
    ) -> PublicRecruitmentListResponse:
        query = self.filtered(filters, project_id=project_id)
        total = await db.scalar(select(func.count()).select_from(query.subquery()))
        rows = list(
            (
                await db.execute(
                    query.order_by(
                        RecruitmentRevision.created_at.desc(),
                        RecruitmentDraft.id.desc(),
                    )
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        )
        return PublicRecruitmentListResponse(
            items=await self.responses(db, rows),
            total=total or 0,
            limit=limit,
            offset=offset,
        )

    async def list_visible(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        filters: PublicProjectFilters,
        limit: int,
        offset: int,
    ) -> PublicRecruitmentListResponse:
        async with mutation(db):
            await PublicAchievementService()._active(db, actor)
            return await self.page(db, filters=filters, limit=limit, offset=offset)

    async def get_visible(
        self, db: AsyncSession, *, actor: Actor, recruitment_id: UUID
    ) -> PublicRecruitmentResponse:
        async with mutation(db):
            await PublicAchievementService()._active(db, actor)
            row = (
                await db.execute(
                    self.query().where(RecruitmentDraft.id == recruitment_id)
                )
            ).one_or_none()
            if row is None:
                raise not_found()
            return (await self.responses(db, [row]))[0]

    async def get_contacts(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        recruitment_id: UUID,
        context: AuditContext,
    ) -> RecruitmentContactsResponse:
        async with mutation(db):
            parent = (
                await db.execute(
                    select(ProjectDraft.id, ProjectDraft.owner_user_id)
                    .join(
                        RecruitmentDraft, RecruitmentDraft.project_id == ProjectDraft.id
                    )
                    .where(RecruitmentDraft.id == recruitment_id)
                )
            ).one_or_none()
            users = {actor.user_id}
            if parent is not None:
                users.add(parent.owner_user_id)
            accounts = (
                await db.execute(
                    select(_USERS)
                    .where(_USERS.c.id.in_(users))
                    .order_by(_USERS.c.id)
                    .with_for_update()
                )
            ).all()
            viewer = next((u for u in accounts if u.id == actor.user_id), None)
            if viewer is None or viewer.role != actor.role or viewer.status != "ACTIVE":
                raise BusinessError(
                    ErrorCode.PERMISSION_DENIED, "账号当前不可用", status_code=403
                )
            if parent is None:
                raise not_found()
            await db.scalar(
                select(OwnerQualification)
                .where(OwnerQualification.user_id == parent.owner_user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            await db.scalar(
                select(ProjectDraft)
                .where(ProjectDraft.id == parent.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            # After all scope locks, re-evaluate eligibility rather than trusting
            # the preliminary lookup. Contact values stay inside this transaction.
            row = (
                await db.execute(
                    self.query()
                    .where(RecruitmentDraft.id == recruitment_id)
                    .with_for_update(of=RecruitmentDraft)
                )
            ).one_or_none()
            if row is None:
                raise not_found()
            contacts = list(
                await db.scalars(
                    select(RecruitmentContactAuthorization)
                    .where(
                        RecruitmentContactAuthorization.revision_id == row.revision_id,
                        RecruitmentContactAuthorization.revoked_at.is_(None),
                    )
                    .order_by(RecruitmentContactAuthorization.channel)
                    .with_for_update()
                )
            )
            await self._audit.append(
                db,
                actor=actor,
                action="ie.recruitment.contacts_read",
                target_type="ie_recruitment",
                target_id=str(recruitment_id),
                details={
                    "revision_number": row.number,
                    "channels": [c.channel for c in contacts],
                    "accessed_at": self._clock.now().isoformat(),
                },
                request_id=context.request_id,
                ip_address=context.ip_address,
            )
            return RecruitmentContactsResponse(
                id=row.id,
                revision_number=row.number,
                contacts=[
                    ContactInput(channel=cast(ContactChannel, c.channel), value=c.value)
                    for c in contacts
                ],
            )
