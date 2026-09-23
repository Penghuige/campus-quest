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

from app.modules.audit.context import AuditContext
from app.modules.identity.dependencies import require_active_staff_actor
from app.modules.identity.events import Actor
from app.modules.identity.providers import (
    AppSettings,
    DbSession,
    LimiterDep,
    get_staff_service,
)
from app.modules.identity.routing_common import (
    _enforce_rate_limit,
    _token_pair_response,
)
from app.modules.identity.schemas import (
    StaffInvitationAcceptRequest,
    StaffLoginRequest,
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


@router.post("/auth/staff/invitations/accept", response_model=TokenPairResponse)
async def accept_staff_invitation(
    body: StaffInvitationAcceptRequest,
    response: Response,
    request: Request,
    staff: Annotated[StaffService, Depends(get_staff_service)],
    settings: AppSettings,
    db: DbSession,
) -> TokenPairResponse:
    """Trade the single-use invitation token for a pending staff session.

    The returned tokens are a real session confined to finishing TOTP
    setup (§5.8); management endpoints stay closed until 2FA is confirmed.
    The accept writes its durable audit row inside the service's
    transaction, carrying this request's correlation pair (§30).
    """
    pending = await staff.accept_staff_invitation(
        db,
        body.token,
        body.password,
        audit_context=AuditContext.from_request(request),
    )
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
