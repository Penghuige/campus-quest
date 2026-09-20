# backend/app/modules/identity/profile_router.py
"""Own-account profile routes (spec §5.4-5.5, §40; backend-engineering §3).

Single responsibility: the authenticated owner's view of and changes to
their own account — profile read, nickname, phone change (OTP to the new
number), email bind/verify/unbind, and password change. Every route is
thin; services and providers come from ``providers``, shared plumbing
from ``routing_common``.

Route-side phone normalization is transport-only: the phone-change route
normalizes the phone through ``otp.normalize_phone`` purely to key the
rate limiter; the service still owns validation and re-normalizes for
storage (single authority unchanged).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.modules.identity.dependencies import (
    get_access_session_id,
    get_actor,
    require_active_actor,
)
from app.modules.identity.email_verification import EmailVerificationService
from app.modules.identity.events import Actor
from app.modules.identity.otp import normalize_phone
from app.modules.identity.profile_service import ProfileService
from app.modules.identity.providers import (
    DbSession,
    LimiterDep,
    get_email_verification_service,
    get_phone_region,
    get_profile_service,
)
from app.modules.identity.routing_common import _client_ip, _enforce_rate_limit
from app.modules.identity.schemas import (
    ChallengeResponse,
    EmailBindRequest,
    EmailChallengeResponse,
    EmailUnbindRequest,
    EmailVerifyRequest,
    MePublic,
    NicknameUpdateRequest,
    PasswordChangeRequest,
    PhoneChangeConfirmRequest,
    PhoneChangeRequest,
)

router = APIRouter()


@router.get("/me", response_model=MePublic)
async def read_me(
    actor: Annotated[Actor, Depends(get_actor)],
    profile: Annotated[ProfileService, Depends(get_profile_service)],
    db: DbSession,
) -> MePublic:
    """The owner's own account view (spec §40). Read-only: any live session
    may read its own profile, including a suspended account's."""
    user = await profile.profile(db, actor.user_id)
    return MePublic.from_user(user)


@router.patch("/me/nickname", response_model=MePublic)
async def change_nickname(
    body: NicknameUpdateRequest,
    actor: Annotated[Actor, Depends(require_active_actor)],
    profile: Annotated[ProfileService, Depends(get_profile_service)],
    db: DbSession,
) -> MePublic:
    user = await profile.change_nickname(db, actor.user_id, body.nickname)
    return MePublic.from_user(user)


@router.post("/me/phone/change", response_model=ChallengeResponse)
async def request_phone_change(
    body: PhoneChangeRequest,
    request: Request,
    actor: Annotated[Actor, Depends(require_active_actor)],
    profile: Annotated[ProfileService, Depends(get_profile_service)],
    limiter: LimiterDep,
    region: Annotated[str, Depends(get_phone_region)],
    db: DbSession,
) -> ChallengeResponse:
    """Re-authenticate, then OTP-challenge the NEW phone (spec §5.4)."""
    new_phone = normalize_phone(body.new_phone, region)
    await _enforce_rate_limit(limiter, "me:phone-change", new_phone)
    challenge = await profile.request_phone_change(
        db,
        actor.user_id,
        body.password,
        body.new_phone,
        client_ip=_client_ip(request),
    )
    return ChallengeResponse(
        challenge_id=challenge.challenge_id, expires_at=challenge.expires_at
    )


@router.post("/me/phone/change/confirm", response_model=MePublic)
async def confirm_phone_change(
    body: PhoneChangeConfirmRequest,
    actor: Annotated[Actor, Depends(require_active_actor)],
    profile: Annotated[ProfileService, Depends(get_profile_service)],
    db: DbSession,
) -> MePublic:
    user = await profile.confirm_phone_change(
        db, actor.user_id, body.challenge_id, body.code
    )
    return MePublic.from_user(user)


@router.post("/me/email", response_model=EmailChallengeResponse)
async def request_email_verification(
    body: EmailBindRequest,
    actor: Annotated[Actor, Depends(require_active_actor)],
    email_service: Annotated[
        EmailVerificationService, Depends(get_email_verification_service)
    ],
    limiter: LimiterDep,
    db: DbSession,
) -> EmailChallengeResponse:
    await _enforce_rate_limit(limiter, "me:email-verify", body.email.strip().lower())
    challenge = await email_service.request_email_verification(
        db, actor.user_id, body.email
    )
    return EmailChallengeResponse(expires_at=challenge.expires_at)


@router.post("/me/email/verify", response_model=MePublic)
async def confirm_email_verification(
    body: EmailVerifyRequest,
    actor: Annotated[Actor, Depends(require_active_actor)],
    email_service: Annotated[
        EmailVerificationService, Depends(get_email_verification_service)
    ],
    db: DbSession,
) -> MePublic:
    user = await email_service.confirm_email_verification(db, actor.user_id, body.token)
    return MePublic.from_user(user)


@router.post("/me/email/unbind", status_code=204)
async def unbind_email(
    body: EmailUnbindRequest,
    actor: Annotated[Actor, Depends(require_active_actor)],
    email_service: Annotated[
        EmailVerificationService, Depends(get_email_verification_service)
    ],
    db: DbSession,
) -> None:
    await email_service.unbind_email(db, actor.user_id, body.password)


@router.post("/me/password", status_code=204)
async def change_password(
    body: PasswordChangeRequest,
    actor: Annotated[Actor, Depends(require_active_actor)],
    session_id: Annotated[uuid.UUID, Depends(get_access_session_id)],
    profile: Annotated[ProfileService, Depends(get_profile_service)],
    db: DbSession,
) -> None:
    """Rotate the password; every other session dies, this one survives."""
    await profile.change_password(
        db,
        actor.user_id,
        body.current_password,
        body.new_password,
        current_session_id=session_id,
    )
