"""Authenticated school-demo browsing, without private identity or evidence."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.modules.identity.dependencies import require_active_actor
from app.modules.identity.events import Actor
from app.modules.innovation.public_achievement_service import PublicAchievementService
from app.modules.innovation.review_schemas import (
    PublicAchievementListResponse,
    PublicAchievementResponse,
)
from app.modules.innovation.router import Database, _private_response

router = APIRouter(
    prefix="/ie/achievements",
    tags=["innovation achievements"],
    dependencies=[Depends(_private_response)],
)
Viewer = Annotated[Actor, Depends(require_active_actor)]


def get_public_achievement_service() -> PublicAchievementService:
    return PublicAchievementService()


Service = Annotated[PublicAchievementService, Depends(get_public_achievement_service)]


@router.get("", response_model=PublicAchievementListResponse)
async def list_visible(
    db: Database,
    actor: Viewer,
    service: Service,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PublicAchievementListResponse:
    return await service.list_visible(db, actor=actor, limit=limit, offset=offset)


@router.get("/{achievement_id}", response_model=PublicAchievementResponse)
async def detail(
    achievement_id: UUID, db: Database, actor: Viewer, service: Service
) -> PublicAchievementResponse:
    return await service.get_visible(db, actor=actor, achievement_id=achievement_id)
