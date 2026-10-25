"""Public project aggregates from approved achievements and effective recruitments."""

from typing import Any
from uuid import UUID

from sqlalchemy import Select, case, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.modules.identity.events import Actor
from app.modules.innovation.models import AchievementDraft, ProjectDraft
from app.modules.innovation.public_achievement_service import PublicAchievementService
from app.modules.innovation.public_recruitment_service import (
    PublicRecruitmentService,
    pattern,
)
from app.modules.innovation.recruitment_models import (
    RecruitmentDraft,
    RecruitmentRevision,
)
from app.modules.innovation.recruitment_schemas import (
    PublicProjectDetailResponse,
    PublicProjectFilters,
    PublicProjectListResponse,
    PublicProjectSummary,
)
from app.modules.innovation.recruitment_service import mutation, not_found
from app.modules.innovation.review_models import (
    AchievementRevision,
    AchievementWorkflow,
)
from app.modules.innovation.review_schemas import PublicAchievementListResponse


class PublicProjectService:
    def __init__(self, *, clock: Clock) -> None:
        self._recruitments = PublicRecruitmentService(clock=clock)
        self._achievements = PublicAchievementService()

    def query(self, filters: PublicProjectFilters) -> Select[Any]:
        # Both source queries select immutable whitelist content, never the
        # parent's latest title/summary, owner profile or pending achievement.
        ac = AchievementRevision.project_content
        achievements = (
            select(
                AchievementDraft.project_id.label("project_id"),
                ac["title"].astext.label("title"),
                ac["summary"].astext.label("summary"),
                ac["direction"].astext.label("direction"),
                ac["stage"].astext.label("stage"),
                literal("").label("role_name"),
                literal(0).label("priority"),
                literal("ACHIEVEMENT").label("source_kind"),
                AchievementWorkflow.latest_update_at.label("stamp"),
                AchievementWorkflow.achievement_id.label("source_id"),
            )
            .select_from(AchievementWorkflow)
            .join(
                AchievementDraft,
                AchievementDraft.id == AchievementWorkflow.achievement_id,
            )
            .join(
                AchievementRevision,
                AchievementRevision.id == AchievementWorkflow.public_revision_id,
            )
            .where(
                AchievementWorkflow.first_review_state == "APPROVED",
                AchievementWorkflow.moderation_state == "NORMAL",
            )
        )
        rc = RecruitmentRevision.content
        recruitments = self._recruitments.query().with_only_columns(
            RecruitmentDraft.project_id.label("project_id"),
            rc["project_title"].astext.label("title"),
            rc["introduction"].astext.label("summary"),
            rc["direction"].astext.label("direction"),
            rc["stage"].astext.label("stage"),
            rc["role_name"].astext.label("role_name"),
            literal(1).label("priority"),
            literal("RECRUITMENT").label("source_kind"),
            RecruitmentRevision.created_at.label("stamp"),
            RecruitmentDraft.id.label("source_id"),
        )
        sources = union_all(achievements, recruitments).cte("public_project_sources")
        ranked = select(
            sources,
            func.row_number()
            .over(
                partition_by=sources.c.project_id,
                order_by=(
                    sources.c.priority,
                    sources.c.stamp.desc(),
                    sources.c.source_id.desc(),
                ),
            )
            .label("rank"),
        ).subquery()
        counts = (
            select(
                sources.c.project_id,
                func.sum(case((sources.c.priority == 0, 1), else_=0)).label(
                    "achievement_count"
                ),
                func.sum(case((sources.c.priority == 1, 1), else_=0)).label(
                    "recruitment_count"
                ),
            )
            .group_by(sources.c.project_id)
            .subquery()
        )
        query = (
            select(
                ranked.c.project_id.label("id"),
                ranked.c.title,
                ranked.c.summary,
                ranked.c.direction,
                ranked.c.stage,
                ranked.c.source_kind,
                counts.c.achievement_count,
                counts.c.recruitment_count,
                ranked.c.stamp,
            )
            .join(counts, counts.c.project_id == ranked.c.project_id)
            .where(ranked.c.rank == 1)
        )
        if filters.q or filters.direction or filters.stage:
            matching = select(sources.c.project_id).where(
                sources.c.project_id == ranked.c.project_id
            )
            if filters.q:
                matching = matching.where(
                    or_(
                        *(
                            sources.c[name].ilike(pattern(filters.q), escape="\\")
                            for name in ("title", "summary", "role_name")
                        )
                    )
                )
            for name in ("direction", "stage"):
                value = getattr(filters, name)
                if value:
                    matching = matching.where(
                        sources.c[name].ilike(pattern(value), escape="\\")
                    )
            query = query.where(matching.exists())
        if filters.recruiting_only:
            query = query.where(counts.c.recruitment_count > 0)
        if filters.beginner_friendly is not None:
            beginner = self._recruitments.filtered(
                PublicProjectFilters(beginner_friendly=filters.beginner_friendly)
            ).where(RecruitmentDraft.project_id == ranked.c.project_id)
            query = query.where(beginner.exists())
        return query

    @staticmethod
    def summary(row: Any) -> PublicProjectSummary:
        return PublicProjectSummary(
            id=row.id,
            title=row.title,
            summary=row.summary,
            direction=row.direction,
            stage=row.stage,
            source_kind=row.source_kind,
            achievement_count=row.achievement_count,
            recruitment_count=row.recruitment_count,
        )

    async def list_visible(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        filters: PublicProjectFilters,
        limit: int,
        offset: int,
    ) -> PublicProjectListResponse:
        async with mutation(db):
            await self._achievements._active(db, actor)
            query = self.query(filters)
            total = await db.scalar(select(func.count()).select_from(query.subquery()))
            rows = (
                await db.execute(
                    query.order_by(
                        query.selected_columns.stamp.desc(),
                        query.selected_columns.id.desc(),
                    )
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            return PublicProjectListResponse(
                items=[self.summary(row) for row in rows],
                total=total or 0,
                limit=limit,
                offset=offset,
            )

    async def get_visible(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        achievement_limit: int = 20,
        achievement_offset: int = 0,
        recruitment_limit: int = 20,
        recruitment_offset: int = 0,
    ) -> PublicProjectDetailResponse:
        async with mutation(db):
            await self._achievements._active(db, actor)
            # Owner writes also lock the project. Re-read all projections after
            # acquiring it so the section counts and sources share this scope.
            parent = await db.scalar(
                select(ProjectDraft.id)
                .where(ProjectDraft.id == project_id)
                .with_for_update()
            )
            if parent is None:
                raise not_found()
            query = self.query(PublicProjectFilters())
            row = (
                await db.execute(query.where(query.selected_columns.id == project_id))
            ).one_or_none()
            if row is None:
                raise not_found()
            aq = (
                self._achievements._query()
                .join(
                    AchievementDraft,
                    AchievementDraft.id == AchievementWorkflow.achievement_id,
                )
                .where(AchievementDraft.project_id == project_id)
            )
            achievements = (
                await db.execute(
                    aq.order_by(
                        AchievementWorkflow.latest_update_at.desc(),
                        AchievementWorkflow.achievement_id.desc(),
                    )
                    .limit(achievement_limit)
                    .offset(achievement_offset)
                )
            ).all()
            return PublicProjectDetailResponse(
                project=self.summary(row),
                achievements=PublicAchievementListResponse(
                    items=[self._achievements._response(item) for item in achievements],
                    total=row.achievement_count,
                    limit=achievement_limit,
                    offset=achievement_offset,
                ),
                recruitments=await self._recruitments.page(
                    db,
                    filters=PublicProjectFilters(),
                    project_id=project_id,
                    limit=recruitment_limit,
                    offset=recruitment_offset,
                ),
            )
