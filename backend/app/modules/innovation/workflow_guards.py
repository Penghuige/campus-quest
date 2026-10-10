"""Shared pending-review edit guard, called under the project/child locks."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.innovation.review_models import (
    AchievementReviewCase,
    AchievementWorkflow,
)


async def require_editable(db: AsyncSession, achievement_id: UUID) -> None:
    state = await db.scalar(
        select(AchievementWorkflow.first_review_state).where(
            AchievementWorkflow.achievement_id == achievement_id
        )
    )
    pending = await db.scalar(
        select(AchievementReviewCase.id).where(
            AchievementReviewCase.achievement_id == achievement_id,
            AchievementReviewCase.status == "SUBMITTED",
        )
    )
    if state == "SUBMITTED" or pending is not None:
        raise BusinessError(
            ErrorCode.CONFLICT, "成果正在核实，请先撤回再修改", status_code=409
        )
