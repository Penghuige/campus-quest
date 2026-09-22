# backend/app/modules/identity/admin_router.py
"""The Admin account-governance API (Plan 08 T9; spec §5.1, §5.7, §5.8,
§33.4): whitelist administration, account status transitions, the staff
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
  refuse peers outside the allowlist with the same 403 envelope.

Transport decisions:

- **AuditContext threads through every write** (§30): each mutating
  route derives ``AuditContext.from_request(request)`` and hands it to
  the service, so the service's in-transaction audit row carries the
  request correlation pair.
- **Listings are offset-paginated with the family cap (limit <= 50)**
  and are read-only ``SELECT``s built inline (the community-router
  precedent for listing endpoints): whitelist entries and the user
  directory need no stateful service logic, so the route owns the query
  and the explicit DTO the same way ``community``'s listings do.
- **The user directory DTO is minimal (G11):** id, username, nickname,
  role, status, created_at — governance facts. Contact fields
  (phone/email) stay behind their own identity surfaces; the filters
  are role/status only.
- **Whitelist preview carries the raw import text as a JSON string.**
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

from datetime import datetime
from typing import Annotated
from uuid import UUID

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
from app.db.session import get_db_session
from app.modules.audit.context import AuditContext
from app.modules.identity.account_admin_service import AccountAdminService
from app.modules.identity.dependencies import get_business_clock, require_admin_actor
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import StudentWhitelist, User
from app.modules.identity.providers import DbSession, LimiterDep, get_staff_service
from app.modules.identity.routing_common import _enforce_rate_limit
from app.modules.identity.schemas import StaffInvitationCreateRequest
from app.modules.identity.staff_service import StaffService
from app.modules.identity.whitelist_admin import (
    WhitelistAdminService,
    WhitelistConfirmPayload,
)
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
    without a restart, and the write-time cross-key validation on the
    settings surface (system router) keeps every stored combination
    loadable, so this guard can never meet a 500-worthy policy.
    """
    settings_service = SystemSettingService()

    async def load_policy() -> ManagementNetworkPolicy:
        stored_enabled = await settings_service.get(db, MANAGEMENT_NETWORK_ENABLED)
        stored_cidrs = await settings_service.get(db, MANAGEMENT_NETWORK_CIDRS)
        return load_management_network_policy(
            stored_enabled=stored_enabled,
            stored_cidrs=stored_cidrs,
        )

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


def get_whitelist_admin_service(
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> WhitelistAdminService:
    # One business-time source per request, shared with the actor guard.
    return WhitelistAdminService(clock=clock)


def get_account_admin_service() -> AccountAdminService:
    return AccountAdminService()


WhitelistAdminDep = Annotated[
    WhitelistAdminService, Depends(get_whitelist_admin_service)
]
AccountAdminDep = Annotated[AccountAdminService, Depends(get_account_admin_service)]
StaffServiceDep = Annotated[StaffService, Depends(get_staff_service)]
AdminActor = Annotated[Actor, Depends(require_admin_actor)]

PageLimit = Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)]
PageOffset = Annotated[int, Query(ge=0)]


# --- transport DTOs (explicit field sets) --------------------------------


class WhitelistEntryResponse(BaseModel):
    """One whitelist row: the number plus its enabled state."""

    model_config = ConfigDict(extra="forbid")

    id: str
    student_number: str
    enabled: bool
    created_at: datetime
    disabled_at: datetime | None


class WhitelistListResponse(BaseModel):
    """Offset-paginated whitelist page, number-ordered."""

    model_config = ConfigDict(extra="forbid")

    items: list[WhitelistEntryResponse]
    total: int
    limit: int
    offset: int


class WhitelistPreviewRequest(BaseModel):
    """The raw import text (one student number per line; the service
    owns decode, trimming, and every row verdict)."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1)


class WhitelistRowDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_number: int
    code: str
    student_number: str | None


class WhitelistCountsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_rows: int
    importable: int
    duplicate_in_file: int
    duplicate_in_db: int
    full_width_digits: int
    invalid_characters: int
    invalid_length: int


class WhitelistPreviewResponse(BaseModel):
    """The preview verdict: per-row decisions, counts, and the digest
    the confirm payload must carry back verbatim."""

    model_config = ConfigDict(extra="forbid")

    total_rows: int
    decisions: list[WhitelistRowDecisionResponse]
    counts: WhitelistCountsResponse
    importable: list[str]
    confirm_token: str


class WhitelistConfirmRequest(BaseModel):
    """The previewed importable set plus its digest (all-or-nothing)."""

    model_config = ConfigDict(extra="forbid")

    confirm_token: str = Field(min_length=1)
    enable: bool = True
    student_numbers: list[str] = Field(min_length=1)


class WhitelistConfirmResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    created: int
    enable: bool


class WhitelistToggleRequest(BaseModel):
    """Flip one entry (enable or disable); the free-text why rides the
    per-entry audit rows."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    reason: str | None = None


class WhitelistToggleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    student_number: str
    toggled: bool


class AdminUserResponse(BaseModel):
    """One account row for the governance listing (G11: governance
    facts only — contact fields stay behind their own surfaces)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    username: str
    nickname: str
    role: Role
    status: UserStatus
    created_at: datetime


class AdminUserListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AdminUserResponse]
    total: int
    limit: int
    offset: int


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


# --- whitelist administration (spec §5.1; W2 services) -------------------


@router.get("/admin/whitelist", response_model=WhitelistListResponse)
async def list_whitelist(
    db: DbSession,
    limit: PageLimit = DEFAULT_PAGE_LIMIT,
    offset: PageOffset = 0,
) -> WhitelistListResponse:
    """The whitelist page, ordered by student number (read-only listing
    built inline — the community-router listing precedent)."""
    total = int(await db.scalar(select(func.count()).select_from(StudentWhitelist)))
    rows = await db.scalars(
        select(StudentWhitelist)
        .order_by(StudentWhitelist.student_number)
        .limit(limit)
        .offset(offset)
    )
    return WhitelistListResponse(
        items=[
            WhitelistEntryResponse(
                id=str(entry.id),
                student_number=entry.student_number,
                enabled=entry.enabled,
                created_at=entry.created_at,
                disabled_at=entry.disabled_at,
            )
            for entry in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/admin/whitelist/preview", response_model=WhitelistPreviewResponse)
async def preview_whitelist_import(
    body: WhitelistPreviewRequest,
    actor: AdminActor,
    db: DbSession,
    service: WhitelistAdminDep,
) -> WhitelistPreviewResponse:
    """Classify every non-blank line; zero writes (preview-then-confirm,
    plan Global Constraints). The digest binds the confirm payload."""
    preview = await service.preview_whitelist_import(
        db, actor, body.content.encode("utf-8")
    )
    return WhitelistPreviewResponse(
        total_rows=preview.total_rows,
        decisions=[
            WhitelistRowDecisionResponse(
                row_number=decision.row_number,
                code=decision.code.value,
                student_number=decision.student_number,
            )
            for decision in preview.decisions
        ],
        counts=WhitelistCountsResponse(
            total_rows=preview.counts.total_rows,
            importable=preview.counts.importable,
            duplicate_in_file=preview.counts.duplicate_in_file,
            duplicate_in_db=preview.counts.duplicate_in_db,
            full_width_digits=preview.counts.full_width_digits,
            invalid_characters=preview.counts.invalid_characters,
            invalid_length=preview.counts.invalid_length,
        ),
        importable=list(preview.importable),
        confirm_token=preview.confirm_token,
    )


@router.post("/admin/whitelist/confirm", response_model=WhitelistConfirmResponse)
async def confirm_whitelist_import(
    body: WhitelistConfirmRequest,
    actor: AdminActor,
    db: DbSession,
    service: WhitelistAdminDep,
    request: Request,
) -> WhitelistConfirmResponse:
    """Insert exactly the previewed set, all-or-nothing; a digest
    mismatch or a DB collision is the typed 409 ``CONFLICT``."""
    payload = WhitelistConfirmPayload(
        confirm_token=body.confirm_token,
        enable=body.enable,
        student_numbers=tuple(body.student_numbers),
    )
    result = await service.confirm_whitelist_import(
        db,
        actor,
        payload,
        audit_context=AuditContext.from_request(request),
    )
    return WhitelistConfirmResponse(created=result.created, enable=result.enable)


@router.patch(
    "/admin/whitelist/{student_number}", response_model=WhitelistToggleResponse
)
async def toggle_whitelist_entry(
    student_number: str,
    body: WhitelistToggleRequest,
    actor: AdminActor,
    db: DbSession,
    service: WhitelistAdminDep,
    request: Request,
) -> WhitelistToggleResponse:
    """Flip one entry to enabled/disabled; an unknown number is the
    typed 400 with the missing list, an idempotent flip changes
    nothing."""
    result = await service.set_entries_enabled(
        db,
        actor,
        [student_number],
        enabled=body.enabled,
        reason=body.reason,
        audit_context=AuditContext.from_request(request),
    )
    return WhitelistToggleResponse(
        student_number=student_number,
        toggled=student_number in result.toggled,
    )


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
