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
from app.modules.identity.dependencies import (
    require_active_staff_actor,
)
from app.modules.identity.events import Actor
from app.modules.identity.providers import (
    AppSettings,
    DbSession,
    get_staff_service,
)
from app.modules.identity.routing_common import (
    _issue_session_cookies,
)
from app.modules.identity.schemas import (
    StaffInvitationAcceptRequest,
    StaffSessionResponse,
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
