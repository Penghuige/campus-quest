# backend/app/modules/identity/auth_router.py
"""Student authentication routes (spec §5; backend-engineering §3).

Single responsibility: the unauthenticated entry surface — phone OTP
challenges, registration, student login, refresh rotation, logout, and
password reset. Every route is thin — validate transport input, call a
service, serialize the public DTO — and owns no persistence logic. The
cookie/CSRF helpers, rate-limit enforcement, typed-exception mapping,
and service providers come from ``routing_common`` / ``providers``.

Route-side phone normalization is transport-only: the OTP-send route
normalizes the phone through ``otp.normalize_phone`` purely to key the
rate limiter; the service still owns validation and re-normalizes for
storage (single authority unchanged).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.modules.identity.otp import OtpPurpose, normalize_phone
from app.modules.identity.profile_service import ProfileService
from app.modules.identity.providers import (
    AppSettings,
    DbSession,
    LimiterDep,
    OtpServiceDep,
    SessionsDep,
    get_identity_service,
    get_phone_region,
    get_profile_service,
)
from app.modules.identity.routing_common import (
    _AUTH_COOKIE_PATH,
    CSRF_COOKIE_NAME,
    REFRESH_COOKIE_NAME,
    _client_ip,
    _enforce_rate_limit,
    _resolve_refresh_token,
    _token_pair_response,
    require_csrf_when_cookie_bearer,
)
from app.modules.identity.schemas import (
    ChallengeResponse,
    LoginRequest,
    LogoutRequest,
    PasswordForgotRequest,
    PasswordResetRequest,
    PhoneChallengeRequest,
    PhoneChallengeVerifyRequest,
    PhoneTokenResponse,
    RefreshRequest,
    RegisterRequest,
    RegisterStudent,
    TokenPairResponse,
    UserPublic,
)
from app.modules.identity.service import IdentityService

router = APIRouter()


@router.post("/auth/phone/challenges", response_model=ChallengeResponse)
async def request_phone_challenge(
    body: PhoneChallengeRequest,
    request: Request,
    otp: OtpServiceDep,
    limiter: LimiterDep,
    region: Annotated[str, Depends(get_phone_region)],
) -> ChallengeResponse:
    """Issue a REGISTER-purpose OTP challenge (spec §5.4, §33.2).

    The purpose is fixed server-side: a public endpoint must never mint
    proofs for password reset or phone change.
    """
    phone = normalize_phone(body.phone, region)
    await _enforce_rate_limit(limiter, "auth:otp-send", phone)
    challenge = await otp.request_phone_challenge(
        body.phone, OtpPurpose.REGISTER, client_ip=_client_ip(request)
    )
    return ChallengeResponse(
        challenge_id=challenge.challenge_id, expires_at=challenge.expires_at
    )


@router.post(
    "/auth/phone/challenges/{challenge_id}/verify", response_model=PhoneTokenResponse
)
async def verify_phone_challenge(
    challenge_id: uuid.UUID,
    body: PhoneChallengeVerifyRequest,
    otp: OtpServiceDep,
) -> PhoneTokenResponse:
    """Consume the challenge with its SMS code; mint the single-use proof."""
    verified = await otp.verify_phone_challenge(challenge_id, body.code)
    return PhoneTokenResponse(
        phone_token=verified.token, expires_at=verified.expires_at
    )


@router.post("/auth/register", response_model=UserPublic, status_code=201)
async def register_student(
    body: RegisterRequest,
    identity: Annotated[IdentityService, Depends(get_identity_service)],
    limiter: LimiterDep,
    db: DbSession,
) -> UserPublic:
    await _enforce_rate_limit(limiter, "auth:register", body.student_number.strip())
    user = await identity.register_student(
        db,
        RegisterStudent(
            student_number=body.student_number,
            nickname=body.nickname,
            phone_token=body.phone_token,
            password=body.password,
        ),
    )
    return UserPublic.from_user(user)


@router.post("/auth/login", response_model=TokenPairResponse)
async def login(
    body: LoginRequest,
    response: Response,
    sessions: SessionsDep,
    settings: AppSettings,
    limiter: LimiterDep,
    db: DbSession,
) -> TokenPairResponse:
    await _enforce_rate_limit(limiter, "auth:login", body.username.strip().lower())
    tokens = await sessions.login_student(db, body.username, body.password)
    return _token_pair_response(response, tokens, settings)


@router.post(
    "/auth/refresh",
    response_model=TokenPairResponse,
    dependencies=[Depends(require_csrf_when_cookie_bearer)],
)
async def refresh(
    request: Request,
    response: Response,
    sessions: SessionsDep,
    settings: AppSettings,
    db: DbSession,
    body: RefreshRequest | None = None,
) -> TokenPairResponse:
    """Rotate one refresh session (cookie or body token; spec §5.6).

    The body is optional: a cookie-authenticated browser sends no body at
    all (the CSRF header carries the mutation proof), so requiring one
    would 422 the exact client this endpoint serves.
    """
    refresh_token = _resolve_refresh_token(
        request, body.refresh_token if body else None
    )
    tokens = await sessions.rotate_refresh(db, refresh_token)
    return _token_pair_response(response, tokens, settings)


@router.post(
    "/auth/logout",
    status_code=204,
    dependencies=[Depends(require_csrf_when_cookie_bearer)],
)
async def logout(
    request: Request,
    response: Response,
    sessions: SessionsDep,
    db: DbSession,
    body: LogoutRequest | None = None,
) -> None:
    """Revoke exactly the presented session; clear the auth cookies.

    Body optional for the same reason as refresh: cookie clients may send
    none.
    """
    refresh_token = _resolve_refresh_token(
        request, body.refresh_token if body else None
    )
    await sessions.revoke_session(db, refresh_token)
    response.delete_cookie(REFRESH_COOKIE_NAME, path=_AUTH_COOKIE_PATH)
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")


@router.post("/auth/password/forgot", response_model=ChallengeResponse)
async def request_password_reset(
    body: PasswordForgotRequest,
    request: Request,
    profile: Annotated[ProfileService, Depends(get_profile_service)],
    limiter: LimiterDep,
    db: DbSession,
) -> ChallengeResponse:
    """Reset OTP to the bound phone — uniformly for EVERY identifier class.

    Known student, unknown username, staff account, phoneless student: the
    response is the identical challenge shape either way (no enumeration
    oracle; see ProfileService.request_password_reset).
    """
    await _enforce_rate_limit(
        limiter, "auth:password-reset", body.username.strip().lower()
    )
    challenge = await profile.request_password_reset(
        db, body.username, client_ip=_client_ip(request)
    )
    return ChallengeResponse(
        challenge_id=challenge.challenge_id, expires_at=challenge.expires_at
    )


@router.post("/auth/password/reset", status_code=204)
async def confirm_password_reset(
    body: PasswordResetRequest,
    profile: Annotated[ProfileService, Depends(get_profile_service)],
    db: DbSession,
) -> None:
    await profile.confirm_password_reset(
        db, body.challenge_id, body.code, body.new_password
    )
