"""School-account projection: select only visible immutable public fields."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Row, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.events import Actor
from app.modules.innovation.operations_service import _USERS
from app.modules.innovation.review_models import (
    AchievementRevision,
    AchievementWorkflow,
)
from app.modules.innovation.review_schemas import (
    PublicAchievementContent,
    PublicAchievementListResponse,
    PublicAchievementResponse,
    PublicProjectContent,
)

PublicProjection = tuple[
    UUID, datetime | None, datetime | None, dict[str, str], dict[str, str], str
]


class PublicAchievementService:
    async def _active(self, db: AsyncSession, actor: Actor) -> None:
        account = (
            await db.execute(
                select(_USERS.c.role, _USERS.c.status)
                .where(_USERS.c.id == actor.user_id)
                .with_for_update()
            )
        ).one_or_none()
        if account is None or account.status != "ACTIVE" or account.role != actor.role:
            raise BusinessError(
                ErrorCode.PERMISSION_DENIED, "账号当前不可用", status_code=403
            )

    @staticmethod
    def _query() -> Select[PublicProjection]:
        return (
            select(
                AchievementWorkflow.achievement_id.label("id"),
                AchievementWorkflow.first_approved_at,
                AchievementWorkflow.latest_update_at,
                AchievementRevision.project_content,
                AchievementRevision.achievement_content,
                AchievementRevision.operation,
            )
            .join(
                AchievementRevision,
                AchievementWorkflow.public_revision_id == AchievementRevision.id,
            )
            .where(
                AchievementWorkflow.first_review_state == "APPROVED",
                AchievementWorkflow.moderation_state == "NORMAL",
            )
        )

    @staticmethod
    def _response(row: Row[PublicProjection]) -> PublicAchievementResponse:
        return PublicAchievementResponse(
            id=row.id,
            project=PublicProjectContent(**row.project_content),
            achievement=PublicAchievementContent(**row.achievement_content),
            first_approved_at=row.first_approved_at,
            updated_at=row.latest_update_at,
            updated_after_first_review=row.operation == "UPDATE",
        )

    async def list_visible(
        self, db: AsyncSession, *, actor: Actor, limit: int, offset: int
    ) -> PublicAchievementListResponse:
        await self._active(db, actor)
        query = self._query()
        total = await db.scalar(select(func.count()).select_from(query.subquery()))
        rows = (
            await db.execute(
                query.order_by(
                    AchievementWorkflow.latest_update_at.desc(),
                    AchievementWorkflow.achievement_id.desc(),
                )
                .limit(limit)
                .offset(offset)
                .with_for_update(of=AchievementWorkflow)
            )
        ).all()
        result = PublicAchievementListResponse(
            items=[self._response(row) for row in rows],
            total=total or 0,
            limit=limit,
            offset=offset,
        )
        await db.commit()
        return result

    async def get_visible(
        self, db: AsyncSession, *, actor: Actor, achievement_id: UUID
    ) -> PublicAchievementResponse:
        await self._active(db, actor)
        row = (
            await db.execute(
                self._query()
                .where(AchievementWorkflow.achievement_id == achievement_id)
                .with_for_update(of=AchievementWorkflow)
            )
        ).one_or_none()
        if row is None:
            raise BusinessError(
                ErrorCode.NOT_FOUND, "成果不存在或当前不可见", status_code=404
            )
        result = self._response(row)
        await db.commit()
        return result
