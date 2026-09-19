# backend/app/modules/identity/router.py
"""Identity HTTP API: thin routes + the module's composition root (Task 9).

Spec §5 (identity flows), §28 (``/api/v1`` prefix), §29 (envelope), §33.1
(cookies/CSRF/rate limit), §33.4 (management 2FA); backend-engineering §3
(router standard), §9 (DTO separation), §16 (security boundary).

Every route is thin — validate transport input, call a service, serialize
the public DTO — and owns no persistence logic (§3). The heavier design
decisions live here:

- **Typed-exception mapping, registered once.** The services raise typed
  module exceptions (``otp``'s taxonomy, ``TotpSetupRequiredError``,
  ``PasswordResetNotAllowedError``, the email errors) because the §29
  registry had no codes when they landed. This module registers one
  FastAPI exception handler per type, rendering the frozen envelope with
  the doc-first codes registered in docs/architecture/interfaces.md
  ("Identity typed-exception mapping"). Routes never translate errors.
- **Refresh token cookie + CSRF double-submit.** Login/rotation set the
  refresh token as an HttpOnly + Secure + SameSite=Lax cookie scoped to
  the auth paths (spec §5.6 推荐), together with a NON-HttpOnly CSRF
  cookie carrying an independent secure random token. Cookie-authenticated
  mutations (refresh, logout) must echo that token in ``X-CSRF-Token``;
  the comparison is constant-time. Non-browser clients send the refresh
  token in the body instead and never receive the CSRF obligation — the
  dependency only engages when the refresh cookie is present, so a
  body-token request carries no ambient cookie authority to forge.
  ``TokenPairResponse.csrf_token`` mirrors the cookie so scripted clients
  need not parse ``Set-Cookie``.
- **Endpoint rate limiting (spec §33.1).** login / staff-login / register
  / OTP-send / email-verify / phone-change / password-reset check the
  ``RateLimiter`` port with NORMALIZED identifiers (stripped+lowercased
  username/email, E.164 phone, stripped student number) before the
  service call, using the shared rules in
  ``app.integrations.rate_limit.RATE_LIMIT_RULES``. Exhausted windows
  render the 429 ``RATE_LIMITED`` envelope. This layers on top of the OTP
  lifecycle's own caps (§33.2); it does not replace them.
- **Route-side phone normalization is transport-only.** The OTP-send and
  phone-change routes normalize the phone through ``otp.normalize_phone``
  purely to key the rate limiter; the service still owns validation and
  re-normalizes for storage (single authority unchanged).
- **Providers are the module composition root.** The Redis client is
  process-cached; services are assembled per request from injected clock,
  settings, codec, and sender dependencies, so tests override a
  dependency, never service internals (backend-engineering §11, §21).
  Real SMS/Email provider adapters arrive with the notification module
  (Plan 07); until then the interim logging adapters record masked
  deliveries (never ``variables`` — the OTP code and email token travel
  there).
- **Staff TOTP setup sits behind ``require_active_staff_actor``**: staff
  role plus ACTIVE status, but no confirmed-TOTP requirement — a pending
  staff session is a real authenticated session that may finish its own
  2FA setup, while every management endpoint keeps the stricter
  ``require_staff_management_actor`` gate (§5.8 step 3, §33.4).
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Annotated

import redis.asyncio as aioredis
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request as StarletteRequest

from app.core.clock import Clock
from app.core.config import Settings, get_settings
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError, error_envelope
from app.core.observability import REQUEST_ID_HEADER
from app.core.security import AccessTokenCodec, constant_time_equals, hash_password
from app.db.session import get_db_session
from app.integrations.email import EmailSender, LoggingEmailSender
from app.integrations.rate_limit import (
    RATE_LIMIT_RULES,
    RateLimiter,
    RateLimitExceededError,
    RedisFixedWindowLimiter,
)
from app.integrations.sms import LoggingSmsSender, SmsSender
from app.modules.identity.dependencies import (
    get_access_session_id,
    get_access_token_codec,
    get_actor,
    get_business_clock,
    require_active_actor,
    require_active_staff_actor,
)
from app.modules.identity.email_verification import (
    EmailAlreadyBoundError,
    EmailVerificationService,
    InvalidEmailTokenError,
)
from app.modules.identity.events import (
    Actor,
    DomainEventPublisher,
    LoggingEventPublisher,
)
from app.modules.identity.otp import (
    ChallengeAlreadyConsumedError,
    ChallengeExpiredError,
    InvalidPhoneError,
    InvalidTokenError,
    OtpChallengeService,
    OtpPolicy,
    OtpPurpose,
    OtpRateLimitError,
    ResendCooldownError,
    TooManyAttemptsError,
    UnknownChallengeError,
    WrongCodeError,
    normalize_phone,
)
from app.modules.identity.profile_service import (
    PasswordResetNotAllowedError,
    ProfileService,
)
from app.modules.identity.schemas import (
    ChallengeResponse,
    EmailBindRequest,
    EmailChallengeResponse,
    EmailUnbindRequest,
    EmailVerifyRequest,
    LoginRequest,
    LogoutRequest,
    MePublic,
    NicknameUpdateRequest,
    PasswordChangeRequest,
    PasswordForgotRequest,
    PasswordResetRequest,
    PhoneChallengeRequest,
    PhoneChallengeVerifyRequest,
    PhoneChangeConfirmRequest,
    PhoneChangeRequest,
    PhoneTokenResponse,
    RefreshRequest,
    RegisterRequest,
    RegisterStudent,
    StaffInvitationAcceptRequest,
    StaffLoginRequest,
    TokenPairResponse,
    TotpConfirmRequest,
    TotpConfirmResponse,
    TotpSetupResponse,
    UserPublic,
)
from app.modules.identity.service import IdentityService
from app.modules.identity.session_service import SessionService, SessionTokens
from app.modules.identity.staff_service import StaffService, TotpSetupRequiredError

logger = logging.getLogger(__name__)

# --- Cookie / CSRF contract (spec §5.6, §33.1) --------------------------------

REFRESH_COOKIE_NAME = "refresh_token"
CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"
# The refresh cookie is only ever read by the auth endpoints; scoping its
# path shrinks what any other endpoint (or bug) can see.
_AUTH_COOKIE_PATH = "/api/v1/auth"
_SECONDS_PER_DAY = 86400

_AUTHENTICATION_REQUIRED_MESSAGE = "未登录或登录状态已失效"
_CSRF_REJECTED_MESSAGE = "CSRF 校验失败：Cookie 请求必须携带有效的 X-CSRF-Token"

# Guard for the TOTP setup endpoints: staff role plus ACTIVE status, but NO
# confirmed-TOTP requirement (these endpoints establish it — the management
# guard would deadlock onboarding). Suspended staff with a live token are
# refused here too (§5.7 gate on every staff action; fix round 1).
_staff_setup_guard = require_active_staff_actor


def require_csrf_when_cookie_bearer(request: Request) -> None:
    """Reject cookie-authenticated mutations without a matching CSRF token.

    Engages ONLY when the refresh cookie is presented: that cookie is the
    ambient browser credential a cross-site forge can carry. A request that
    authenticates purely by body (mobile clients) involves no ambient
    authority and skips the check. The double-submit pair is the non-HttpOnly
    ``csrf_token`` cookie and the ``X-CSRF-Token`` header, compared
    constant-time; absence on either side is the same rejection.
    """
    if REFRESH_COOKIE_NAME not in request.cookies:
        return
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    header_token = request.headers.get(CSRF_HEADER_NAME)
    if (
        not cookie_token
        or not header_token
        or not constant_time_equals(cookie_token, header_token)
    ):
        raise BusinessError(
            ErrorCode.PERMISSION_DENIED,
            _CSRF_REJECTED_MESSAGE,
            status_code=403,
        )


def _issue_session_cookies(
    response: Response, tokens: SessionTokens, settings: Settings
) -> str:
    """Set the refresh (HttpOnly) and CSRF (readable) cookies; return CSRF.

    Both cookies rotate with every issue, so a stale CSRF token from a
    previous session cannot authorize the next refresh. Spec §5.6/§33.1:
    Secure + SameSite=Lax on both; HttpOnly on the refresh cookie only —
    the CSRF token is double-submit material and must stay readable.
    """
    csrf_token = secrets.token_urlsafe(32)
    max_age = settings.refresh_token_ttl_days * _SECONDS_PER_DAY
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        tokens.refresh_token,
        max_age=max_age,
        secure=True,
        httponly=True,
        samesite="lax",
        path=_AUTH_COOKIE_PATH,
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        max_age=max_age,
        secure=True,
        samesite="lax",
        path="/",
    )
    return csrf_token


def _token_pair_response(
    response: Response, tokens: SessionTokens, settings: Settings
) -> TokenPairResponse:
    csrf_token = _issue_session_cookies(response, tokens, settings)
    return TokenPairResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        csrf_token=csrf_token,
    )


def _resolve_refresh_token(request: Request, body_token: str | None) -> str:
    """The presented refresh token: body first, then the cookie, else 401."""
    if body_token:
        return body_token
    cookie_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if cookie_token:
        return cookie_token
    raise BusinessError(
        ErrorCode.AUTHENTICATION_REQUIRED,
        _AUTHENTICATION_REQUIRED_MESSAGE,
        status_code=401,
    )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


# --- Typed-exception -> envelope mapping (docs/architecture/interfaces.md) ----

_TYPED_EXCEPTION_CODES: tuple[tuple[type[Exception], int, ErrorCode], ...] = (
    (TotpSetupRequiredError, 403, ErrorCode.TOTP_SETUP_REQUIRED),
    (PasswordResetNotAllowedError, 403, ErrorCode.PASSWORD_RESET_NOT_ALLOWED),
    (EmailAlreadyBoundError, 409, ErrorCode.EMAIL_ALREADY_BOUND),
    (InvalidEmailTokenError, 400, ErrorCode.INVALID_EMAIL_TOKEN),
    (InvalidPhoneError, 400, ErrorCode.VALIDATION_ERROR),
    (WrongCodeError, 400, ErrorCode.OTP_CODE_INVALID),
    (TooManyAttemptsError, 429, ErrorCode.OTP_TOO_MANY_ATTEMPTS),
    (ChallengeExpiredError, 400, ErrorCode.OTP_CHALLENGE_EXPIRED),
    (ChallengeAlreadyConsumedError, 400, ErrorCode.OTP_CHALLENGE_CONSUMED),
    (UnknownChallengeError, 400, ErrorCode.OTP_CHALLENGE_INVALID),
    (InvalidTokenError, 400, ErrorCode.OTP_TOKEN_INVALID),
    (ResendCooldownError, 429, ErrorCode.OTP_RESEND_COOLDOWN),
    # Both rate-limit failures (OTP-internal caps and the endpoint limiter)
    # render the same code; the branch reason stays in server logs.
    (OtpRateLimitError, 429, ErrorCode.RATE_LIMITED),
    (RateLimitExceededError, 429, ErrorCode.RATE_LIMITED),
)


def register_identity_exception_handlers(app: FastAPI) -> None:
    """Attach one §29-envelope handler per typed identity exception.

    Rendered through the same envelope builder as ``core.errors`` so every
    response — business error, typed domain error, or framework failure —
    is indistinguishable in shape. Messages come from the exceptions (their
    Chinese UX strings), codes from the frozen registry.
    """

    def _handler(
        status_code: int, code: ErrorCode
    ) -> Callable[[StarletteRequest, Exception], Awaitable[JSONResponse]]:
        async def render(request: StarletteRequest, exc: Exception) -> JSONResponse:
            request_id = getattr(request.state, "request_id", None)
            headers = {REQUEST_ID_HEADER: request_id} if request_id else None
            return JSONResponse(
                status_code=status_code,
                content=error_envelope(code, str(exc), None, request_id),
                headers=headers,
            )

        return render

    for exception_type, status_code, code in _TYPED_EXCEPTION_CODES:
        app.add_exception_handler(exception_type, _handler(status_code, code))


# --- Provider dependencies (module composition root) --------------------------


@lru_cache
def get_identity_redis() -> aioredis.Redis:
    """Process-wide Redis client for OTP/email/rate-limit state.

    ``decode_responses=True`` keeps replies as ``str`` (every consumer
    decodes anyway); the connection pool is shared per process. Services
    receive the client through ``Depends(get_identity_redis)`` — a direct
    call inside a provider would dodge ``dependency_overrides``, which is
    exactly the seam integration tests use to point at the flushed test
    database.
    """
    return aioredis.from_url(get_settings().redis_url, decode_responses=True)


RedisDep = Annotated[aioredis.Redis, Depends(get_identity_redis)]


def get_rate_limiter(
    clock: Annotated[Clock, Depends(get_business_clock)],
    redis: RedisDep,
) -> RateLimiter:
    return RedisFixedWindowLimiter(redis=redis, clock=clock)


def get_sms_sender() -> SmsSender:
    """Interim adapter (Plan 07 replaces with a real provider)."""
    return LoggingSmsSender()


def get_email_sender() -> EmailSender:
    """Interim adapter (Plan 07 replaces with a real provider)."""
    return LoggingEmailSender()


def get_event_publisher() -> DomainEventPublisher:
    """Interim adapter: log the event, persist nothing (Plan 08 audit)."""
    return LoggingEventPublisher()


def get_otp_service(
    sms_sender: Annotated[SmsSender, Depends(get_sms_sender)],
    clock: Annotated[Clock, Depends(get_business_clock)],
    settings: Annotated[Settings, Depends(get_settings)],
    redis: RedisDep,
) -> OtpChallengeService:
    return OtpChallengeService(
        redis=redis,
        clock=clock,
        sms_sender=sms_sender,
        policy=OtpPolicy.from_settings(settings),
    )


def get_session_service(
    clock: Annotated[Clock, Depends(get_business_clock)],
    codec: Annotated[AccessTokenCodec, Depends(get_access_token_codec)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SessionService:
    return SessionService(
        clock=clock,
        access_codec=codec,
        refresh_token_ttl_days=settings.refresh_token_ttl_days,
    )


def get_identity_service(
    otp: Annotated[OtpChallengeService, Depends(get_otp_service)],
) -> IdentityService:
    return IdentityService(password_hasher=hash_password, phone_verification=otp)


def get_profile_service(
    otp: Annotated[OtpChallengeService, Depends(get_otp_service)],
    clock: Annotated[Clock, Depends(get_business_clock)],
    sessions: Annotated[SessionService, Depends(get_session_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ProfileService:
    return ProfileService(
        clock=clock,
        otp=otp,
        otp_policy=OtpPolicy.from_settings(settings),
        sessions=sessions,
        phone_default_region=settings.phone_default_region,
    )


def get_email_verification_service(
    email_sender: Annotated[EmailSender, Depends(get_email_sender)],
    clock: Annotated[Clock, Depends(get_business_clock)],
    settings: Annotated[Settings, Depends(get_settings)],
    redis: RedisDep,
) -> EmailVerificationService:
    return EmailVerificationService(
        clock=clock,
        email_sender=email_sender,
        redis=redis,
        token_ttl_hours=settings.email_verification_token_ttl_hours,
    )


def get_staff_service(
    clock: Annotated[Clock, Depends(get_business_clock)],
    sessions: Annotated[SessionService, Depends(get_session_service)],
    settings: Annotated[Settings, Depends(get_settings)],
    events: Annotated[DomainEventPublisher, Depends(get_event_publisher)],
) -> StaffService:
    return StaffService(
        clock=clock,
        sessions=sessions,
        fernet=Fernet(settings.totp_encryption_key),
        events=events,
        invitation_ttl_hours=settings.staff_invitation_ttl_hours,
    )


def get_phone_region(
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    return settings.phone_default_region


DbSession = Annotated[AsyncSession, Depends(get_db_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]
OtpServiceDep = Annotated[OtpChallengeService, Depends(get_otp_service)]
SessionsDep = Annotated[SessionService, Depends(get_session_service)]
LimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]


async def _enforce_rate_limit(
    limiter: RateLimiter, bucket: str, identifier: str
) -> None:
    rule = RATE_LIMIT_RULES[bucket]
    await limiter.check(
        bucket=rule.bucket,
        identifier=identifier,
        limit=rule.limit,
        window_seconds=rule.window_seconds,
    )


# --- Routes (spec §5, §28) -----------------------------------------------------

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


@router.post("/auth/staff/login", response_model=TokenPairResponse)
async def staff_login(
    body: StaffLoginRequest,
    response: Response,
    staff: Annotated[StaffService, Depends(get_staff_service)],
    settings: AppSettings,
    limiter: LimiterDep,
    db: DbSession,
) -> TokenPairResponse:
    """Staff login: verified email + password + TOTP-or-recovery (§5.8).

    A correct password without a confirmed TOTP raises the typed setup
    error, rendered as 403 ``TOTP_SETUP_REQUIRED`` — the client's signal to
    finish ``/staff/totp/*`` first.
    """
    await _enforce_rate_limit(limiter, "auth:staff-login", body.email.strip().lower())
    tokens = await staff.authenticate_staff(
        db, body.email, body.password, body.totp_code
    )
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
    """Send a reset OTP to the bound phone — uniformly, known user or not."""
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


@router.post("/auth/staff/invitations/accept", response_model=TokenPairResponse)
async def accept_staff_invitation(
    body: StaffInvitationAcceptRequest,
    response: Response,
    staff: Annotated[StaffService, Depends(get_staff_service)],
    settings: AppSettings,
    db: DbSession,
) -> TokenPairResponse:
    """Trade the single-use invitation token for a pending staff session.

    The returned tokens are a real session confined to finishing TOTP
    setup (§5.8); management endpoints stay closed until 2FA is confirmed.
    """
    pending = await staff.accept_staff_invitation(db, body.token, body.password)
    return _token_pair_response(response, pending.tokens, settings)


@router.post("/staff/totp/begin", response_model=TotpSetupResponse)
async def begin_totp_setup(
    actor: Annotated[Actor, Depends(_staff_setup_guard)],
    staff: Annotated[StaffService, Depends(get_staff_service)],
    db: DbSession,
) -> TotpSetupResponse:
    """Generate (or rotate) the pending TOTP secret; displayed once (§5.8)."""
    setup = await staff.begin_totp_setup(db, actor.user_id)
    return TotpSetupResponse(secret=setup.secret, otpauth_uri=setup.otpauth_uri)


@router.post("/staff/totp/confirm", response_model=TotpConfirmResponse)
async def confirm_totp_setup(
    body: TotpConfirmRequest,
    actor: Annotated[Actor, Depends(_staff_setup_guard)],
    staff: Annotated[StaffService, Depends(get_staff_service)],
    db: DbSession,
) -> TotpConfirmResponse:
    """Confirm with one valid code; recovery codes shown exactly once."""
    codes = await staff.confirm_totp_setup(db, actor.user_id, body.code)
    return TotpConfirmResponse(recovery_codes=codes)


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
