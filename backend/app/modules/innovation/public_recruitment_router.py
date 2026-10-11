"""Authenticated campus browsing and rate-limited deliberate contact reads."""

from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Query, Request
from redis.exceptions import RedisError

from app.core.clock import Clock
from app.core.config import get_settings
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.integrations.rate_limit import (
    RATE_LIMIT_RULES,
    RateLimiter,
    RateLimitExceededError,
    RedisFixedWindowLimiter,
)
from app.modules.audit.context import AuditContext
from app.modules.identity.dependencies import get_business_clock
from app.modules.innovation.public_achievement_router import Viewer
from app.modules.innovation.public_project_service import PublicProjectService
from app.modules.innovation.public_recruitment_service import PublicRecruitmentService
from app.modules.innovation.recruitment_schemas import (
    PublicProjectDetailResponse,
    PublicProjectFilters,
    PublicProjectListResponse,
    PublicRecruitmentListResponse,
    PublicRecruitmentResponse,
    RecruitmentContactsResponse,
)
from app.modules.innovation.router import Database, _private_response

router = APIRouter(
    prefix="/ie",
    tags=["innovation public recruitment"],
    dependencies=[Depends(_private_response)],
)
ClockDep = Annotated[Clock, Depends(get_business_clock)]
Filters = Annotated[PublicProjectFilters, Query()]


def get_public_recruitment_service(clock: ClockDep) -> PublicRecruitmentService:
    return PublicRecruitmentService(clock=clock)


def get_public_project_service(clock: ClockDep) -> PublicProjectService:
    return PublicProjectService(clock=clock)


async def get_contact_rate_limiter(clock: ClockDep) -> AsyncIterator[RateLimiter]:
    async with aioredis.from_url(
        get_settings().redis_url, decode_responses=True
    ) as redis:
        yield RedisFixedWindowLimiter(redis=redis, clock=clock)


Recruitments = Annotated[
    PublicRecruitmentService, Depends(get_public_recruitment_service)
]
Projects = Annotated[PublicProjectService, Depends(get_public_project_service)]
Limiter = Annotated[RateLimiter, Depends(get_contact_rate_limiter)]


@router.get("/recruitments")
async def list_recruitments(
    db: Database, actor: Viewer, service: Recruitments, filters: Filters
) -> PublicRecruitmentListResponse:
    return await service.list_visible(
        db, actor=actor, filters=filters, limit=filters.limit, offset=filters.offset
    )


@router.get("/recruitments/{recruitment_id}")
async def get_recruitment(
    recruitment_id: UUID, db: Database, actor: Viewer, service: Recruitments
) -> PublicRecruitmentResponse:
    return await service.get_visible(db, actor=actor, recruitment_id=recruitment_id)


@router.get("/recruitments/{recruitment_id}/contacts")
async def get_contacts(
    recruitment_id: UUID,
    request: Request,
    db: Database,
    actor: Viewer,
    service: Recruitments,
    limiter: Limiter,
) -> RecruitmentContactsResponse:
    rule = RATE_LIMIT_RULES["ie:recruitment-contacts"]
    try:
        await limiter.check(
            bucket=rule.bucket,
            identifier=str(actor.user_id),
            limit=rule.limit,
            window_seconds=rule.window_seconds,
        )
    except RateLimitExceededError as exc:
        raise BusinessError(
            ErrorCode.RATE_LIMITED, "联系读取过于频繁，请稍后重试", status_code=429
        ) from exc
    except RedisError as exc:
        raise BusinessError(
            ErrorCode.RECRUITMENT_CONTACTS_UNAVAILABLE,
            "联系读取暂不可用，请稍后重试",
            status_code=503,
        ) from exc
    return await service.get_contacts(
        db,
        actor=actor,
        recruitment_id=recruitment_id,
        context=AuditContext.from_request(request),
    )


@router.get("/projects")
async def list_projects(
    db: Database, actor: Viewer, service: Projects, filters: Filters
) -> PublicProjectListResponse:
    return await service.list_visible(
        db, actor=actor, filters=filters, limit=filters.limit, offset=filters.offset
    )


@router.get("/projects/{project_id}")
async def get_project(
    project_id: UUID,
    db: Database,
    actor: Viewer,
    service: Projects,
    achievement_limit: Annotated[int, Query(ge=1, le=50)] = 20,
    achievement_offset: Annotated[int, Query(ge=0)] = 0,
    recruitment_limit: Annotated[int, Query(ge=1, le=50)] = 20,
    recruitment_offset: Annotated[int, Query(ge=0)] = 0,
) -> PublicProjectDetailResponse:
    return await service.get_visible(
        db,
        actor=actor,
        project_id=project_id,
        achievement_limit=achievement_limit,
        achievement_offset=achievement_offset,
        recruitment_limit=recruitment_limit,
        recruitment_offset=recruitment_offset,
    )
