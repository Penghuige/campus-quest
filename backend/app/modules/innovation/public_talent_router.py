"""Campus talent transport, explicit contact reads and private byte downloads."""

from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Query, Request, Response
from redis.exceptions import RedisError

from app.core.clock import Clock
from app.core.config import get_settings
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.integrations.object_storage_s3 import S3ObjectStorage
from app.integrations.rate_limit import (
    RATE_LIMIT_RULES,
    RateLimiter,
    RateLimitExceededError,
    RedisFixedWindowLimiter,
)
from app.modules.audit.context import AuditContext
from app.modules.identity.dependencies import get_business_clock
from app.modules.innovation.dependencies import Database, Viewer, _private_response
from app.modules.innovation.public_talent_service import PublicTalentService
from app.modules.innovation.talent_schemas import (
    PublicTalentListResponse,
    PublicTalentResponse,
    TalentContactsResponse,
    TalentPublicReadCommand,
    TalentQuery,
)

router = APIRouter(
    prefix="/ie/talents",
    tags=["innovation public talents"],
    dependencies=[Depends(_private_response)],
)
ClockDep = Annotated[Clock, Depends(get_business_clock)]


def get_public_talent_service(clock: ClockDep) -> PublicTalentService:
    return PublicTalentService(
        clock=clock, storage=S3ObjectStorage(get_settings(), clock=clock)
    )


async def get_talent_contact_rate_limiter(
    clock: ClockDep,
) -> AsyncIterator[RateLimiter]:
    async with aioredis.from_url(
        get_settings().redis_url, decode_responses=True
    ) as redis:
        yield RedisFixedWindowLimiter(redis=redis, clock=clock)


Service = Annotated[PublicTalentService, Depends(get_public_talent_service)]
Limiter = Annotated[RateLimiter, Depends(get_talent_contact_rate_limiter)]


@router.get("", response_model=PublicTalentListResponse)
async def list_visible(
    db: Database,
    actor: Viewer,
    service: Service,
    query: Annotated[TalentQuery, Query()],
) -> PublicTalentListResponse:
    return await service.list_visible(db, actor=actor, query=query)


@router.get("/{profile_id}", response_model=PublicTalentResponse)
async def detail(
    profile_id: UUID, db: Database, actor: Viewer, service: Service
) -> PublicTalentResponse:
    return await service.get_visible(db, actor=actor, profile_id=profile_id)


@router.post("/{profile_id}/contacts", response_model=TalentContactsResponse)
async def contacts(
    profile_id: UUID,
    payload: TalentPublicReadCommand,
    request: Request,
    db: Database,
    actor: Viewer,
    service: Service,
    limiter: Limiter,
) -> TalentContactsResponse:
    rule = RATE_LIMIT_RULES["ie:talent-contacts"]
    try:
        await limiter.check(
            bucket=rule.bucket,
            identifier=str(actor.user_id),
            limit=rule.limit,
            window_seconds=rule.window_seconds,
        )
    except RateLimitExceededError as error:
        raise BusinessError(
            ErrorCode.RATE_LIMITED, "联系读取过于频繁，请稍后重试", status_code=429
        ) from error
    except RedisError as error:
        raise BusinessError(
            ErrorCode.TALENT_CONTACTS_UNAVAILABLE,
            "联系读取暂不可用，请稍后重试",
            status_code=503,
        ) from error
    return await service.get_contacts(
        db,
        actor=actor,
        profile_id=profile_id,
        expected_public_version=payload.expected_public_version,
        context=AuditContext.from_request(request),
    )


@router.get("/{profile_id}/files/{file_id}/content")
async def content(
    profile_id: UUID,
    file_id: UUID,
    request: Request,
    db: Database,
    actor: Viewer,
    service: Service,
    expected_public_version: Annotated[int, Query(ge=1)],
) -> Response:
    file = await service.read_file_content(
        db,
        actor=actor,
        profile_id=profile_id,
        file_id=file_id,
        expected_public_version=expected_public_version,
        context=AuditContext.from_request(request),
    )
    extension = {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg"}[
        file.content_type
    ]
    return Response(
        file.content,
        media_type=file.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="talent-work.{extension}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )
