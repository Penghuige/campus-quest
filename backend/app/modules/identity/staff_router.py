# backend/app/modules/identity/staff_router.py
"""Staff authentication and mandatory TOTP setup (spec §5.8, §33.4).

Single responsibility: the staff entry surface — staff login (verified
email + password + TOTP-or-recovery), single-use invitation acceptance,
and the TOTP setup endpoints a pending staff session uses to finish its
own 2FA onboarding. Every route is thin; services and providers come
from ``providers``, the session-cookie contract from ``routing_common``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from app.modules.audit.context import AuditContext
from app.modules.identity.dependencies import (
    require_active_staff_actor,
)
from app.modules.identity.device_credentials import (
    DEVICE_COOKIE_NAME,
    DEVICE_TTL_DAYS,
)
from app.modules.identity.email_login_service import EmailLoginService
from app.modules.identity.events import Actor
from app.modules.identity.mobile_login_service import MobileLoginService
from app.modules.identity.providers import (
    AppSettings,
    DbSession,
    LimiterDep,
    get_email_login_service,
    get_mobile_login_service,
    get_staff_service,
)
from app.modules.identity.routing_common import (
    _client_ip,
    _enforce_rate_limit,
    _issue_session_cookies,
    _token_pair_response,
)
from app.modules.identity.schemas import (
    StaffEmailCodePendingResponse,
    StaffEmailCodeRequestRequest,
    StaffEmailCodeTotpRequest,
    StaffEmailCodeVerifyRequest,
    StaffInvitationAcceptRequest,
    StaffLoginRequest,
    StaffMobileCodeRequestRequest,
    StaffMobileCodeTotpRequest,
    StaffMobileCodeVerifyRequest,
    StaffSessionResponse,
    TokenPairResponse,
    TotpConfirmRequest,
    TotpConfirmResponse,
    TotpSetupResponse,
)
from app.modules.identity.staff_service import StaffService

# Guard for the TOTP setup endpoints: staff role plus ACTIVE status, but NO
# confirmed-TOTP requirement (these endpoints establish it — the management
# guard would deadlock onboarding). Suspended staff with a live token are
# refused here too (§5.7 gate on every staff action; fix round 1).
_staff_setup_guard = require_active_staff_actor

router = APIRouter()


@router.post("/auth/staff/login", response_model=TokenPairResponse)
async def staff_login(
    body: StaffLoginRequest,
    request: Request,
    response: Response,
    staff: Annotated[StaffService, Depends(get_staff_service)],
    settings: AppSettings,
    limiter: LimiterDep,
    db: DbSession,
) -> TokenPairResponse:
    """Staff login: verified email + password + TOTP-or-recovery (§5.8).

    A correct password without a confirmed TOTP raises the typed setup
    error, rendered as 403 ``TOTP_SETUP_REQUIRED`` — the client's signal to
    finish ``/staff/totp/*`` first. Two limiter layers (hardening A-1):
    per email and per transport peer.

    V1.1 login iteration item 2: an EMPTY ``totp_code`` completes the
    login when a valid remembered-device cookie rides the request (the
    30-day re-login); ``remember_device`` mints that cookie on a
    successful login.
    """
    await _enforce_rate_limit(
        limiter,
        "auth:staff-login",
        body.email.strip().lower(),
        client_ip=_client_ip(request),
    )
    tokens, device_cookie = await staff.authenticate_staff(
        db,
        body.email,
        body.password,
        body.totp_code,
        device_cookie=request.cookies.get(DEVICE_COOKIE_NAME),
        remember_device=body.remember_device,
    )
    # Strict policy (this legacy route) always resolves tokens; the
    # None shape belongs to the unified form's caller only.
    assert tokens is not None
    pair = _token_pair_response(response, tokens, settings)
    if device_cookie is not None:
        response.set_cookie(
            DEVICE_COOKIE_NAME,
            device_cookie,
            max_age=DEVICE_TTL_DAYS * 24 * 3600,
            secure=True,
            httponly=True,
            samesite="lax",
        )
    return pair


@router.post("/auth/staff/email-code/request", status_code=202)
async def request_staff_email_code(
    body: StaffEmailCodeRequestRequest,
    request: Request,
    email_login: Annotated[EmailLoginService, Depends(get_email_login_service)],
    limiter: LimiterDep,
    db: DbSession,
) -> Response:
    """Mail one 6-digit login code (V1.1 §2.1) — always 202, empty body.

    The response is identical for every mailbox — unknown, student, or
    staff — so the endpoint is not an existence oracle; the service's
    60s cooldown is armed on the raw submitted email for all of them
    alike, and only a cooldown/liter breach (429) differs.
    """
    await _enforce_rate_limit(
        limiter,
        "auth:staff-email-code",
        body.email.strip().lower(),
        client_ip=_client_ip(request),
    )
    await email_login.request_code(db, body.email)
    return JSONResponse({}, status_code=202)


@router.post("/auth/staff/email-code/verify")
async def verify_staff_email_code(
    body: StaffEmailCodeVerifyRequest,
    request: Request,
    response: Response,
    email_login: Annotated[EmailLoginService, Depends(get_email_login_service)],
    settings: AppSettings,
    db: DbSession,
) -> TokenPairResponse | StaffEmailCodePendingResponse:
    """Trade the emailed code for a login (V1.1 §2.1/§2.2).

    TEACHER completes here; ADMIN answers ``totp_required`` with a
    pending session token — unless a valid remembered-device cookie
    accompanies the request, in which case ADMIN finishes here too.
    ``remember_device`` issues the 30-day cookie on whichever leg
    completes the login.
    """
    outcome = await email_login.verify_code(
        db,
        email=body.email,
        code=body.code,
        device_cookie=request.cookies.get(DEVICE_COOKIE_NAME),
        remember_device=body.remember_device,
        audit_context=AuditContext.from_request(request),
    )
    if outcome.pending_token is not None:
        return StaffEmailCodePendingResponse(
            status="totp_required",
            pending_token=outcome.pending_token,
        )
    assert outcome.tokens is not None
    tokens = _token_pair_response(response, outcome.tokens, settings)
    _set_device_cookie(response, outcome.device_cookie)
    return tokens


@router.post("/auth/staff/email-code/totp", response_model=TokenPairResponse)
async def staff_email_code_totp(
    body: StaffEmailCodeTotpRequest,
    request: Request,
    response: Response,
    email_login: Annotated[EmailLoginService, Depends(get_email_login_service)],
    settings: AppSettings,
    db: DbSession,
) -> TokenPairResponse:
    """Finish an ADMIN email-code login with the TOTP leg (V1.1 §2.2).

    ``pending_token`` is the verify leg's OPAQUE mid-login credential
    (Redis, 30-minute TTL, consumed by this call) — it authenticates
    nothing but this endpoint, so a half-finished login holds no session
    power (final review F1).
    """
    outcome = await email_login.verify_totp(
        db,
        pending_token=body.pending_token,
        code=body.code,
        remember_device=body.remember_device,
        audit_context=AuditContext.from_request(request),
    )
    assert outcome.tokens is not None
    tokens = _token_pair_response(response, outcome.tokens, settings)
    _set_device_cookie(response, outcome.device_cookie)
    return tokens


@router.post("/auth/staff/mobile-code/request", status_code=202)
async def request_staff_mobile_code(
    body: StaffMobileCodeRequestRequest,
    request: Request,
    mobile_login: Annotated[MobileLoginService, Depends(get_mobile_login_service)],
    limiter: LimiterDep,
    db: DbSession,
) -> Response:
    """SMS one staff login code (V1.1 login iteration item 1) — always
    202, empty body. The response is identical for every phone —
    unknown, student-bound, or staff — so the endpoint is not an
    existence oracle; a phone that binds no ACTIVE staff account never
    receives a code, and only a cooldown/liter breach (429) differs.
    """
    from app.modules.identity.otp import InvalidPhoneError, normalize_phone

    try:
        limiter_phone = normalize_phone(body.phone, "CN")
    except InvalidPhoneError:
        limiter_phone = body.phone.strip()
    await _enforce_rate_limit(
        limiter,
        "auth:staff-mobile-code",
        limiter_phone,
        client_ip=_client_ip(request),
    )
    await mobile_login.request_staff_code(db, body.phone)
    return JSONResponse({}, status_code=202)


@router.post("/auth/staff/mobile-code/verify")
async def verify_staff_mobile_code(
    body: StaffMobileCodeVerifyRequest,
    request: Request,
    response: Response,
    mobile_login: Annotated[MobileLoginService, Depends(get_mobile_login_service)],
    settings: AppSettings,
    db: DbSession,
) -> TokenPairResponse | StaffEmailCodePendingResponse:
    """Trade the staff SMS code for a login (V1.1 login iteration item 1).

    TEACHER completes here; ADMIN answers ``totp_required`` with the
    opaque pending token — unless a valid remembered-device cookie
    accompanies the (empty-body) request, in which case ADMIN finishes
    here too (the #87 craft). ``remember_device`` issues the 30-day
    cookie on whichever leg completes the login.
    """
    outcome = await mobile_login.verify_staff_code(
        db,
        phone=body.phone,
        code=body.code,
        device_cookie=request.cookies.get(DEVICE_COOKIE_NAME),
        remember_device=body.remember_device,
        audit_context=AuditContext.from_request(request),
    )
    if outcome.pending_token is not None:
        return StaffEmailCodePendingResponse(
            status="totp_required",
            pending_token=outcome.pending_token,
        )
    assert outcome.tokens is not None
    tokens = _token_pair_response(response, outcome.tokens, settings)
    _set_device_cookie(response, outcome.device_cookie)
    return tokens


@router.post("/auth/staff/mobile-code/totp", response_model=TokenPairResponse)
async def staff_mobile_code_totp(
    body: StaffMobileCodeTotpRequest,
    request: Request,
    response: Response,
    mobile_login: Annotated[MobileLoginService, Depends(get_mobile_login_service)],
    settings: AppSettings,
    db: DbSession,
) -> TokenPairResponse:
    """Finish an ADMIN mobile-code login with the TOTP leg — the same
    opaque, single-use, 30-minute pending credential the email-code
    family uses (final review F1 discipline)."""
    tokens, device_cookie = await mobile_login.complete_staff_totp(
        db,
        pending_token=body.pending_token,
        code=body.code,
        remember_device=body.remember_device,
        audit_context=AuditContext.from_request(request),
    )
    pair = _token_pair_response(response, tokens, settings)
    _set_device_cookie(response, device_cookie)
    return pair


def _set_device_cookie(response: Response, cookie_value: str | None) -> None:
    """Set the remembered-device cookie exactly when one was minted.

    HttpOnly + Secure + SameSite=Lax (V1.1 §2.2); no explicit Path — the
    cookie must reach every /api/v1/auth/staff/email-code/* call, and
    the browser default (this host, all paths) is precisely that scope.
    """
    if cookie_value is None:
        return
    response.set_cookie(
        DEVICE_COOKIE_NAME,
        cookie_value,
        max_age=DEVICE_TTL_DAYS * 24 * 3600,
        secure=True,
        httponly=True,
        samesite="lax",
    )


@router.post("/auth/staff/invitations/accept", response_model=StaffSessionResponse)
async def accept_staff_invitation(
    body: StaffInvitationAcceptRequest,
    response: Response,
    request: Request,
    staff: Annotated[StaffService, Depends(get_staff_service)],
    settings: AppSettings,
    db: DbSession,
) -> StaffSessionResponse:
    """Trade the single-use invitation token for a staff session.

    V1.1 §2.3: a TEACHER acceptance is a COMPLETE login (optional
    password, no TOTP onboarding — ``totp_required: false``); an ADMIN
    acceptance is the V1 pending-TOTP onboarding session
    (``totp_required: true``; management endpoints stay closed until 2FA
    is confirmed). The accept writes its durable audit row inside the
    service's transaction, carrying this request's correlation pair
    (§30).
    """
    pending = await staff.accept_staff_invitation(
        db,
        body.token,
        body.password,
        audit_context=AuditContext.from_request(request),
    )
    csrf_token = _issue_session_cookies(response, pending.tokens, settings)
    return StaffSessionResponse(
        access_token=pending.tokens.access_token,
        csrf_token=csrf_token,
        totp_required=pending.must_setup_totp,
    )


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
