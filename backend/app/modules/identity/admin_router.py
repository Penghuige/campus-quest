# backend/app/modules/identity/admin_router.py
"""The Admin account-governance API (Plan 08 T9; spec §5.1, §5.7, §5.8,
§33.4): account status governance, the staff invitation family,
directory, and staff invitation issuance — thin routes over the W2/W4
services, never a second implementation of their rules.

Every route in this router mounts the family's guard pair (G10):

- ``require_admin_actor`` — Admin role + ACTIVE + confirmed TOTP (the
  §33.4 management gate). A Teacher or Student calling any URL here
  directly answers ``PERMISSION_DENIED`` 403; the services re-check the
  same invariant as defense in depth.
- ``require_management_network_from_store`` — the management-network
  guard whose policy loader reads the two ``MANAGEMENT_NETWORK_*`` rows
  from the audited settings STORE per request (composed below with
  core's ``require_management_network`` factory and
  ``load_management_network_policy`` — the W4 ``stored_*`` keyword
  seam). This assembly is the W4 footgun's formal closure: the
  transitional env-cached default policy is no longer what production
  requests resolve. Disabled policies pass through; enabled policies
  refuse peers outside the allowlist with the same 403 envelope; an
  UNLOADABLE stored pair fails closed — 403 to every caller plus an
  ERROR log (PR #5 fix A) — while the system-settings router stays
  exempt from the guard as the pair's self-repair face.

Transport decisions:

- **AuditContext threads through every write** (§30): each mutating
  route derives ``AuditContext.from_request(request)`` and hands it to
  the service, so the service's in-transaction audit row carries the
  request correlation pair.
- **Listings are offset-paginated with the family cap (limit <= 50)**
  and are read-only ``SELECT``s built inline (the community-router
  directory need no stateful service logic, so the route owns the query
  and the explicit DTO the same way ``community``'s listings do.
- **The user directory DTO is minimal (G11):** id, username, nickname,
  role, status, created_at — governance facts. Contact fields
  (phone/email) stay behind their own identity surfaces; the filters
  are role/status only.
  The service owns the byte-level decode contract (UTF-8, BOM
  tolerance, size/row caps); the route encodes the payload to UTF-8
  bytes and never inspects rows itself.
- **Staff invitation issuance (plan T9):** ``POST /staff/invitations``
  is ``create_staff_invitation`` mounted, not re-implemented —
  Admin-guarded here, rate-limited through the shared
  ``staff:invitations`` bucket keyed by the inviting Admin's user id,
  audited inside the service's transaction. The one-time token is
  returned exactly once (the row stores only its hash); no email
  delivery of invitations is wired in V1, so the response is the
  admin's only chance to hand the link over.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_network_policy import (
    ManagementNetworkPolicy,
    load_management_network_policy,
    require_management_network,
)
from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.db.session import get_db_session
from app.integrations.masking import mask_phone
from app.modules.audit.context import AuditContext
from app.modules.identity import admin_power_service as admin_power
from app.modules.identity.account_admin_service import AccountAdminService
from app.modules.identity.dependencies import (
    get_business_clock,
    require_admin_actor,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.identity.providers import (
    DbSession,
    LimiterDep,
    get_identity_redis,
    get_session_service,
    get_staff_service,
)
from app.modules.identity.routing_common import _enforce_rate_limit
from app.modules.identity.schemas import (
    AdminPasswordResetLinkResponse,
    StaffInvitationCreateRequest,
)
from app.modules.identity.session_service import SessionService
from app.modules.identity.staff_service import StaffService
from app.modules.system.service import (
    MANAGEMENT_NETWORK_CIDRS,
    MANAGEMENT_NETWORK_ENABLED,
    SystemSettingService,
)

__all__ = [
    "require_management_network_from_store",
    "router",
]

# The documented V1 pagination choice, family cap included (the
# notifications/points router bounds).
DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 50

# The staff-invitation issuance bucket (app/integrations/rate_limit.py).
_INVITATION_BUCKET = "staff:invitations"

logger = logging.getLogger(__name__)

# The fail-closed refusal for an UNLOADABLE stored pair (PR #5 fix A):
# the message names the repair path — the settings surface is exempt
# from this guard, so the Admin can always reach it.
_POLICY_UNLOADABLE_MESSAGE = (
    "管理网络策略配置不可加载，已拒绝所有管理功能访问；请立即通过系统设置修复网络策略"
)


# --- the store-backed management-network guard (W4 footgun closure) ------


async def require_management_network_from_store(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    """The management-network guard with its policy resolved from the
    audited settings store per request.

    Composition only: the CHECK is core's ``require_management_network``
    factory (used, not reimplemented — its trust boundary, fail-closed
    parsing, and disabled-pass-through semantics stay single-sourced),
    and the loader handed to it reads the two ``MANAGEMENT_NETWORK_*``
    rows through ``SystemSettingService.get`` and resolves them with
    ``load_management_network_policy`` (store first, per-key env
    fallback — the W4 migration). Policy changes therefore apply
    without a restart, and the aggregate advisory lock on the write
    path (``SystemSettingService.set_management_network_policy``, PR #5
    fix A) keeps every stored combination loadable.

    **Fail closed on an unloadable pair** (PR #5 fix A, the terminal
    review's ruling): a stored pair the resolver rejects — historical
    residue from before the aggregate lock — is a management incident,
    not a per-request input problem, so this guard answers the 403
    envelope to EVERY caller (in-network and out) after an ERROR log
    naming the offending rows, never a 500. The system-settings router
    is exempt from this guard (the self-repair face), so the Admin can
    always reach the API that fixes the pair.
    """
    settings_service = SystemSettingService()

    async def load_policy() -> ManagementNetworkPolicy:
        stored_enabled = await settings_service.get(db, MANAGEMENT_NETWORK_ENABLED)
        stored_cidrs = await settings_service.get(db, MANAGEMENT_NETWORK_CIDRS)
        try:
            return load_management_network_policy(
                stored_enabled=stored_enabled,
                stored_cidrs=stored_cidrs,
            )
        except ValueError as exc:
            logger.error(
                "management-network policy is unloadable — failing closed "
                "(denying all management access); repair through the "
                "settings surface: stored_enabled=%r stored_cidrs=%r (%s)",
                stored_enabled,
                stored_cidrs,
                exc,
            )
            raise BusinessError(
                ErrorCode.PERMISSION_DENIED,
                _POLICY_UNLOADABLE_MESSAGE,
                status_code=403,
                details={
                    "stored_enabled": stored_enabled,
                    "stored_cidrs": stored_cidrs,
                },
            ) from exc

    guard = require_management_network(load_policy)
    await guard(request)


# The router-wide guard pair (G10): the Admin management gate plus the
# store-backed network restriction.
router = APIRouter(
    dependencies=[
        Depends(require_admin_actor),
        Depends(require_management_network_from_store),
    ],
)


# --- provider dependencies (module composition root) ---------------------


def get_account_admin_service() -> AccountAdminService:
    return AccountAdminService()


AccountAdminDep = Annotated[AccountAdminService, Depends(get_account_admin_service)]
StaffServiceDep = Annotated[StaffService, Depends(get_staff_service)]
AdminActor = Annotated[Actor, Depends(require_admin_actor)]

PageLimit = Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)]
PageOffset = Annotated[int, Query(ge=0)]


# --- transport DTOs (explicit field sets) --------------------------------


class AdminUserResponse(BaseModel):
    """One account row for the governance listing (G11: governance
    facts plus the MASKED phone — the approval queue's identifying
    contact fact, minimal-necessary form; the raw number stays behind
    its own surfaces)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    username: str
    nickname: str
    role: Role
    status: UserStatus
    phone_masked: str | None
    created_at: datetime


class AdminUserListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AdminUserResponse]
    total: int
    limit: int
    offset: int


class AdminRecentSubmission(BaseModel):
    """One row of the panorama's recent-submissions strip (Wave 2a §3.1):
    submission facts only — no content, no URLs, no storage keys."""

    model_config = ConfigDict(extra="forbid")

    id: str
    task_title: str
    validation_status: str
    review_status: str
    submitted_at: datetime


class AdminUserPanoramaResponse(BaseModel):
    """The §3.1 detail view: the governance row plus the cross-module
    facts the console renders on one account page."""

    model_config = ConfigDict(extra="forbid")

    user: AdminUserResponse
    wallet_balance: int
    recent_submissions: list[AdminRecentSubmission]
    active_session_count: int
    remembered_device_count: int


class AdminUserSessionsResponse(BaseModel):
    """The §3.3 read: how many live refresh sessions and remembered
    devices the account currently holds."""

    model_config = ConfigDict(extra="forbid")

    active_session_count: int
    remembered_device_count: int


class AdminKickResponse(BaseModel):
    """The §3.3 stop: what one kick took out."""

    model_config = ConfigDict(extra="forbid")

    revoked_sessions: int
    revoked_devices: int


class AccountStatusRequest(BaseModel):
    """The mandatory free-text why of one status transition (spec §5.7;
    the service refuses blanks before any read)."""

    model_config = ConfigDict(extra="forbid")

    reason: str


class AccountStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    username: str
    role: Role
    status: UserStatus


class StaffInvitationResponse(BaseModel):
    """One issued invitation. ``token`` is the ONE-TIME capability,
    returned exactly here and never again — the row stores only its
    hash, and no invitation email is wired in V1."""

    model_config = ConfigDict(extra="forbid")

    id: str
    role: Role
    token: str
    expires_at: datetime
    created_at: datetime


# --- account status governance (spec §5.7; W2 service) --------------------


@router.get("/admin/users", response_model=AdminUserListResponse)
async def list_users(
    db: DbSession,
    role: Annotated[Role | None, Query()] = None,
    status: Annotated[UserStatus | None, Query()] = None,
    limit: PageLimit = DEFAULT_PAGE_LIMIT,
    offset: PageOffset = 0,
) -> AdminUserListResponse:
    """The account directory page with role/status filters (username
    ordering; read-only listing built inline)."""
    conditions = []
    if role is not None:
        conditions.append(User.role == role.value)
    if status is not None:
        conditions.append(User.status == status.value)
    total = int(
        await db.scalar(select(func.count()).select_from(User).where(*conditions))
    )
    rows = await db.scalars(
        select(User)
        .where(*conditions)
        .order_by(User.username)
        .limit(limit)
        .offset(offset)
    )
    return AdminUserListResponse(
        items=[
            AdminUserResponse(
                id=str(user.id),
                username=user.username,
                nickname=user.nickname,
                role=Role(user.role),
                status=UserStatus(user.status),
                phone_masked=mask_phone(user.phone_e164) if user.phone_e164 else None,
                created_at=user.created_at,
            )
            for user in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


async def _transition_account_status(
    db: AsyncSession,
    actor: Actor,
    service: AccountAdminService,
    user_id: UUID,
    body: AccountStatusRequest,
    request: Request,
    method_name: str,
) -> AccountStatusResponse:
    """Shared body of the three status routes: run the one named
    transition (AuditContext threaded), serialize the resulting row."""
    user = await getattr(service, method_name)(
        db,
        actor,
        user_id,
        reason=body.reason,
        audit_context=AuditContext.from_request(request),
    )
    return AccountStatusResponse(
        id=str(user.id),
        username=user.username,
        role=Role(user.role),
        status=UserStatus(user.status),
    )


@router.get("/admin/users/{user_id}", response_model=AdminUserPanoramaResponse)
async def read_user_panorama(
    user_id: UUID,
    actor: AdminActor,
    db: DbSession,
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> AdminUserPanoramaResponse:
    """The §3.1 account panorama: governance row, wallet balance, the
    ten most recent submissions, and the live credential counts."""
    panorama = await admin_power.user_panorama(db, user_id, clock=clock)
    user = panorama["user"]
    return AdminUserPanoramaResponse(
        user=AdminUserResponse(
            id=str(user.id),
            username=user.username,
            nickname=user.nickname,
            role=Role(user.role),
            status=UserStatus(user.status),
            phone_masked=mask_phone(user.phone_e164) if user.phone_e164 else None,
            created_at=user.created_at,
        ),
        wallet_balance=panorama["wallet_balance"],
        recent_submissions=[
            AdminRecentSubmission(
                id=str(s.id),
                task_title=title,
                validation_status=str(s.validation_status),
                review_status=str(s.review_status),
                submitted_at=s.submitted_at,
            )
            for s, title in panorama["recent_submissions"]
        ],
        active_session_count=panorama["active_session_count"],
        remembered_device_count=panorama["remembered_device_count"],
    )


@router.get("/admin/users/{user_id}/sessions", response_model=AdminUserSessionsResponse)
async def read_user_sessions(
    user_id: UUID,
    actor: AdminActor,
    db: DbSession,
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> AdminUserSessionsResponse:
    """The §3.3 read side: the account's live credential counts."""
    panorama = await admin_power.user_panorama(db, user_id, clock=clock)
    return AdminUserSessionsResponse(
        active_session_count=panorama["active_session_count"],
        remembered_device_count=panorama["remembered_device_count"],
    )


@router.delete("/admin/users/{user_id}/sessions", response_model=AdminKickResponse)
async def kick_user(
    user_id: UUID,
    request: Request,
    actor: AdminActor,
    db: DbSession,
    sessions: Annotated[SessionService, Depends(get_session_service)],
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> AdminKickResponse:
    """The §3.3 stolen-account emergency stop: every live refresh
    session revoked, every remembered-device credential dropped, one
    ADMIN_SESSIONS_REVOKED audit row in the same commit."""
    revoked_sessions, revoked_devices = await admin_power.kick(
        db,
        actor,
        user_id,
        sessions=sessions,
        clock=clock,
        audit_context=AuditContext.from_request(request),
    )
    return AdminKickResponse(
        revoked_sessions=revoked_sessions, revoked_devices=revoked_devices
    )


@router.post(
    "/admin/users/{user_id}/password-reset",
    response_model=AdminPasswordResetLinkResponse,
)
async def issue_password_reset_link(
    user_id: UUID,
    request: Request,
    actor: AdminActor,
    db: DbSession,
    redis: Annotated[aioredis.Redis, Depends(get_identity_redis)],
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> AdminPasswordResetLinkResponse:
    """The §3.2 one-time reset link for ANOTHER staff account: the
    urlsafe32 plaintext shows exactly once (only its SHA-256 is kept,
    48h TTL), ISSUED rides the same commit. Self-service and student
    targets are refused — students recover through their phone OTP."""
    token = await admin_power.issue_password_reset(
        db,
        actor,
        user_id,
        redis=redis,
        clock=clock,
        audit_context=AuditContext.from_request(request),
    )
    return AdminPasswordResetLinkResponse(reset_token=token)


@router.post("/admin/users/{user_id}/suspend", response_model=AccountStatusResponse)
async def suspend_user(
    user_id: UUID,
    body: AccountStatusRequest,
    actor: AdminActor,
    db: DbSession,
    service: AccountAdminDep,
    request: Request,
) -> AccountStatusResponse:
    """ACTIVE -> SUSPENDED (spec §5.7); every other from-state is the
    typed 409 ``CONFLICT`` carrying the observed/requested pair."""
    return await _transition_account_status(
        db, actor, service, user_id, body, request, "suspend_user"
    )


@router.post("/admin/users/{user_id}/ban", response_model=AccountStatusResponse)
async def ban_user(
    user_id: UUID,
    body: AccountStatusRequest,
    actor: AdminActor,
    db: DbSession,
    service: AccountAdminDep,
    request: Request,
) -> AccountStatusResponse:
    """ACTIVE -> BANNED (spec §5.7)."""
    return await _transition_account_status(
        db, actor, service, user_id, body, request, "ban_user"
    )


@router.post("/admin/users/{user_id}/reactivate", response_model=AccountStatusResponse)
async def reactivate_user(
    user_id: UUID,
    body: AccountStatusRequest,
    actor: AdminActor,
    db: DbSession,
    service: AccountAdminDep,
    request: Request,
) -> AccountStatusResponse:
    """SUSPENDED/BANNED -> ACTIVE — the Admin unban included (§5.7)."""
    return await _transition_account_status(
        db, actor, service, user_id, body, request, "reactivate_user"
    )


class ApprovalVerdictResponse(BaseModel):
    """The registration verdict's result row (V1.1 spec 2026-10-16 §2)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    username: str
    status: UserStatus
    approved_at: datetime | None = None
    rejected_reason: str | None = None


class RejectRequest(BaseModel):
    """reason is optional; ≤255 chars (the column bound)."""

    reason: str | None = Field(default=None, max_length=255)


@router.post("/admin/users/{user_id}/approve", response_model=ApprovalVerdictResponse)
async def approve_student(
    user_id: UUID,
    actor: AdminActor,
    db: DbSession,
    service: AccountAdminDep,
    request: Request,
) -> ApprovalVerdictResponse:
    """PENDING_APPROVAL -> ACTIVE (V1.1 spec 2026-10-16 §2): approve a
    student registration; USER_APPROVED rides the same transaction."""
    user = await service.approve_student(
        db, actor, user_id, audit_context=AuditContext.from_request(request)
    )
    return ApprovalVerdictResponse(
        id=str(user.id),
        username=user.username,
        status=UserStatus(user.status),
        approved_at=user.approved_at,
    )


@router.post("/admin/users/{user_id}/reject", response_model=ApprovalVerdictResponse)
async def reject_student(
    user_id: UUID,
    body: RejectRequest,
    actor: AdminActor,
    db: DbSession,
    service: AccountAdminDep,
    request: Request,
) -> ApprovalVerdictResponse:
    """PENDING_APPROVAL -> BANNED with the optional reason (spec §2);
    USER_REJECTED rides the same transaction."""
    user = await service.reject_student(
        db,
        actor,
        user_id,
        reason=body.reason or "",
        audit_context=AuditContext.from_request(request),
    )
    return ApprovalVerdictResponse(
        id=str(user.id),
        username=user.username,
        status=UserStatus(user.status),
        rejected_reason=user.rejected_reason,
    )


# --- staff invitation issuance (spec §5.8; plan T9) -----------------------


@router.post("/staff/invitations", response_model=StaffInvitationResponse)
async def create_staff_invitation(
    body: StaffInvitationCreateRequest,
    actor: AdminActor,
    staff: StaffServiceDep,
    limiter: LimiterDep,
    db: DbSession,
    request: Request,
) -> StaffInvitationResponse:
    """Issue one one-shot staff invitation (Admin-only, rate-limited per
    inviting admin; the service validates the email and the TEACHER/
    ADMIN narrowing and commits the audited row). The token returns
    exactly once."""
    await _enforce_rate_limit(limiter, _INVITATION_BUCKET, str(actor.user_id))
    issued = await staff.create_staff_invitation(
        db,
        actor,
        body.email,
        body.role,
        audit_context=AuditContext.from_request(request),
    )
    return StaffInvitationResponse(
        id=str(issued.invitation.id),
        role=Role(issued.invitation.role),
        token=issued.token,
        expires_at=issued.invitation.expires_at,
        created_at=issued.invitation.created_at,
    )
