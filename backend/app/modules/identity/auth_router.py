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

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from app.core.clock import Clock
from app.integrations.masking import mask_phone
from app.modules.audit.context import AuditContext
from app.modules.identity.dependencies import get_business_clock
from app.modules.identity.device_credentials import (
    DEVICE_COOKIE_NAME,
    DEVICE_TTL_DAYS,
)
from app.modules.identity.device_credentials import (
    validate as validate_device,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.mobile_login_service import MobileLoginService
from app.modules.identity.models import User
from app.modules.identity.otp import (
    OtpPurpose,
    normalize_phone,
)
from app.modules.identity.profile_service import ProfileService
from app.modules.identity.providers import (
    AppSettings,
    DbSession,
    LimiterDep,
    OtpServiceDep,
    SessionsDep,
    get_identity_service,
    get_mobile_login_service,
    get_phone_region,
    get_profile_service,
    get_unified_login_service,
)
from app.modules.identity.providers import (
    get_identity_redis as get_identity_redis_placeholder,
)
from app.modules.identity.routing_common import (
    _client_ip,
    _enforce_rate_limit,
    _resolve_refresh_token,
    _token_pair_response,
    clear_session_cookies,
    require_csrf_when_cookie_bearer,
)
from app.modules.identity.schemas import (
    ChallengeResponse,
    LoginRequest,
    LogoutRequest,
    MobileCodeRegisterRequest,
    PasswordForgotRequest,
    PasswordResetRequest,
    PasswordResetTokenSpendRequest,
    PhoneChallengeRequest,
    PhoneChallengeVerifyRequest,
    PhoneTokenResponse,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    RegisterStudent,
    TokenPairResponse,
    UnifiedAuthOutcome,
    UnifiedChallengeRequest,
    UnifiedChallengeTotpRequest,
    UnifiedChallengeVerifyRequest,
    UserPublic,
)
from app.modules.identity.service import IdentityService
from app.modules.identity.unified_login_service import UnifiedLoginService

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
    await _enforce_rate_limit(
        limiter, "auth:otp-send", phone, client_ip=_client_ip(request)
    )
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
    request: Request,
    otp: OtpServiceDep,
    limiter: LimiterDep,
) -> PhoneTokenResponse:
    """Consume the challenge with its SMS code; mint the single-use proof.

    Rate-limited per challenge id AND per transport peer (hardening A-1):
    the OTP service already bounds wrong-code attempts per challenge, so
    these buckets anti-hammer the HTTP surface itself.
    """
    await _enforce_rate_limit(
        limiter,
        "auth:otp-verify",
        str(challenge_id),
        client_ip=_client_ip(request),
    )
    verified = await otp.verify_phone_challenge(challenge_id, body.code)
    return PhoneTokenResponse(
        phone_token=verified.token, expires_at=verified.expires_at
    )


@router.post("/auth/register", response_model=RegisterResponse, status_code=201)
async def register_student(
    body: RegisterRequest,
    response: Response,
    identity: Annotated[IdentityService, Depends(get_identity_service)],
    sessions: SessionsDep,
    clock: Annotated[Clock, Depends(get_business_clock)],
    settings: AppSettings,
    limiter: LimiterDep,
    db: DbSession,
) -> RegisterResponse:
    """Register one student — and land LOGGED IN (V1.1 login iteration
    item 4): the freshly proved phone makes an immediate re-login (and
    its 60s SMS cooldown) pure friction. The response pairs the account
    view with the same session body/cookie set the login path answers
    (PENDING_APPROVAL accounts browse the frozen home per #96)."""
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
    _, tokens = await sessions.issue_session(db, user=user, now=clock.now())
    await db.commit()
    return RegisterResponse(
        user=UserPublic.from_user(user),
        tokens=_token_pair_response(response, tokens, settings),
    )


@router.post(
    "/auth/mobile-code/register",
    response_model=RegisterResponse,
    status_code=201,
)
async def register_by_mobile_code(
    body: MobileCodeRegisterRequest,
    response: Response,
    mobile_login: Annotated[MobileLoginService, Depends(get_mobile_login_service)],
    sessions: SessionsDep,
    clock: Annotated[Clock, Depends(get_business_clock)],
    settings: AppSettings,
    db: DbSession,
) -> RegisterResponse:
    """Trade the entry proof for a PENDING_APPROVAL account with the
    phone bound — and land LOGGED IN (the register-ends-logged-in
    semantics, the {user, tokens} envelope the classic door answers)."""
    user = await mobile_login.register_by_phone(
        db,
        register_token=body.register_token,
        student_number=body.student_number,
        password=body.password,
        nickname=body.nickname,
    )
    _, tokens = await sessions.issue_session(db, user=user, now=clock.now())
    await db.commit()
    return RegisterResponse(
        user=UserPublic.from_user(user),
        tokens=_token_pair_response(response, tokens, settings),
    )


@router.post("/auth/login", response_model=None)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    sessions: SessionsDep,
    unified: Annotated[UnifiedLoginService, Depends(get_unified_login_service)],
    settings: AppSettings,
    limiter: LimiterDep,
    db: DbSession,
) -> TokenPairResponse | UnifiedAuthOutcome:
    """Student or staff password login — BOTH forms (2026-10-19 §3.2).

    Legacy: {username, password} answers the flat TokenPairResponse
    (unchanged bytes; the retirement PR removes it). Unified:
    {identifier, password, remember_device?} answers the discriminated
    union — a phone/student-number reaches the student flow (phone
    first), an email reaches the staff flow with the remembered-cookie
    honoring preserved; an ADMIN without one answers totp_required and
    the totp leg completes at /auth/challenge/totp.

    PR-3 (2026-10-19 spec §3.2/§3.3): the legacy {username, password}
    form is RETIRED — the unified identifier form is the only shape."""
    identity = (body.identifier or "").strip()
    # Two layers (hardening A-1): per identity and per transport peer.
    await _enforce_rate_limit(
        limiter,
        "auth:login",
        identity.lower(),
        client_ip=_client_ip(request),
    )
    outcome = await unified.login(
        db,
        identifier=body.identifier or "",
        password=body.password,
        device_cookie=request.cookies.get(DEVICE_COOKIE_NAME),
        remember_device=body.remember_device,
    )
    return _unified_outcome_response(response, outcome, settings)


def _unified_outcome_response(
    response: Response, outcome: object, settings: AppSettings
) -> UnifiedAuthOutcome:
    """Shape one facade outcome: tokens ride the refresh-cookie pair
    (the HttpOnly delivery, never the body), a minted device cookie
    rides its own HttpOnly Set-Cookie, and only the union's named
    members reach the body."""
    tokens = getattr(outcome, "tokens", None)
    pair = None
    if tokens is not None:
        pair = _token_pair_response(response, tokens, settings)
    device_cookie = getattr(outcome, "device_cookie", None)
    if device_cookie is not None:
        response.set_cookie(
            DEVICE_COOKIE_NAME,
            device_cookie,
            max_age=DEVICE_TTL_DAYS * 24 * 3600,
            secure=True,
            httponly=True,
            samesite="lax",
        )
    return UnifiedAuthOutcome(
        status=outcome.status,  # type: ignore[attr-defined]
        role=getattr(outcome, "role", None),
        tokens=pair,
        pending_token=getattr(outcome, "pending_token", None),
        register_token=getattr(outcome, "register_token", None),
    )


@router.post("/auth/challenge/request", status_code=202, response_model=None)
async def unified_challenge_request(
    body: UnifiedChallengeRequest,
    request: Request,
    unified: Annotated[UnifiedLoginService, Depends(get_unified_login_service)],
    limiter: LimiterDep,
    db: DbSession,
) -> Response:
    """One code request, routed by identifier shape (2026-10-19 §3.1):
    email → the staff email-code service; phone by the users row → the
    student mobile-code service (unregistered phones included — the
    entry flow) or the staff one. Always the arm's own 202; a student
    number is a format-layer 422 with no account query."""
    await _enforce_rate_limit(
        limiter,
        "auth:challenge",
        body.identifier.strip().lower(),
        client_ip=_client_ip(request),
    )
    await unified.request(db, body.identifier)
    return JSONResponse({}, status_code=202)


@router.post("/auth/challenge/verify", response_model=None)
async def unified_challenge_verify(
    body: UnifiedChallengeVerifyRequest,
    request: Request,
    response: Response,
    unified: Annotated[UnifiedLoginService, Depends(get_unified_login_service)],
    settings: AppSettings,
    db: DbSession,
) -> UnifiedAuthOutcome:
    """The code leg (or, with BOTH fields empty, the remembered-cookie
    single-factor leg): the discriminated union answers authenticated /
    totp_required / unregistered — ``status`` is the only judgment
    member. Wrong or expired or exhausted codes answer the single
    CHALLENGE_CODE_INVALID."""
    outcome = await unified.verify(
        db,
        identifier=body.identifier,
        code=body.code,
        device_cookie=request.cookies.get(DEVICE_COOKIE_NAME),
        remember_device=body.remember_device,
        audit_context=AuditContext.from_request(request),
    )
    return _unified_outcome_response(response, outcome, settings)


@router.post("/auth/challenge/totp", response_model=None)
async def unified_challenge_totp(
    body: UnifiedChallengeTotpRequest,
    request: Request,
    response: Response,
    unified: Annotated[UnifiedLoginService, Depends(get_unified_login_service)],
    settings: AppSettings,
    limiter: LimiterDep,
    db: DbSession,
) -> UnifiedAuthOutcome:
    """The ADMIN second factor — ONE endpoint over BOTH pending domains
    (email-login and staff-mobile; a wrong-domain probe burns nothing).
    Transport-peer rate-limited (interfaces.md: the 6-digit factor under
    online brute force is the login limiter's job; the per-credential
    caps stay underneath)."""
    await _enforce_rate_limit(
        limiter,
        "auth:challenge-totp",
        _client_ip(request) or "unknown",
        client_ip=_client_ip(request),
    )
    outcome = await unified.verify_totp(
        db,
        pending_token=body.pending_token,
        code=body.totp_code,
        remember_device=body.remember_device,
        audit_context=AuditContext.from_request(request),
    )
    return _unified_outcome_response(response, outcome, settings)


@router.get("/auth/challenge/remembered", status_code=200, response_model=None)
async def unified_remembered_probe(
    request: Request,
    clock: Annotated[Clock, Depends(get_business_clock)],
    db: DbSession,
) -> Response:
    """Is a remembered device alive for the login banner? The §3.1
    remembered read, the mobile-code probe's contract: 200
    {phone_masked} only for the banner's audience (a cookie-bound
    STUDENT), the same empty 204 for every other shape."""
    cookie = request.cookies.get(DEVICE_COOKIE_NAME)
    if cookie is not None:
        user_id = await validate_device(db, cookie, clock=clock)
        if user_id is not None:
            user = await db.get(User, user_id)
            if (
                user is not None
                and user.role == Role.STUDENT
                and user.status in (UserStatus.ACTIVE, UserStatus.PENDING_APPROVAL)
                and user.phone_e164
            ):
                return JSONResponse({"phone_masked": mask_phone(user.phone_e164)})
    return Response(status_code=204)


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
    settings: AppSettings,
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
    clear_session_cookies(response, settings)


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


@router.post("/auth/password/reset-token", status_code=204)
async def spend_admin_reset_token(
    body: PasswordResetTokenSpendRequest,
    sessions: SessionsDep,
    clock: Annotated[Clock, Depends(get_business_clock)],
    redis: Annotated[aioredis.Redis, Depends(get_identity_redis_placeholder)],
    db: DbSession,
) -> None:
    """Spend the admin-issued one-time reset link (Wave 2a §3.2): the
    token IS the authorization — this endpoint is public. Sets the new
    password, revokes every live session, writes CONSUMED; the
    credential dies with the GETDEL (a replay answers the uniform
    invalid)."""
    from app.modules.identity import admin_power_service

    await admin_power_service.consume_password_reset(
        db,
        redis=redis,
        reset_token=body.reset_token,
        new_password=body.new_password,
        clock=clock,
        sessions=sessions,
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
