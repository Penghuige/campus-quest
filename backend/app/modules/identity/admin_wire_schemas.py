# backend/app/modules/identity/admin_wire_schemas.py
"""Pydantic wire schemas for the Admin account-governance API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.identity.enums import Role, UserStatus


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


class AdminUserRoleResponse(BaseModel):
    """The §3.4 flip's result row."""

    model_config = ConfigDict(extra="forbid")

    id: str
    username: str
    role: Role
    status: UserStatus


class AdminOverviewUsers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    by_role: dict[str, int]
    by_status: dict[str, int]


class AdminOverviewTasks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by_status: dict[str, int]


class AdminOverviewToday(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_users: int
    active_users: int


class AdminOverviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    users: AdminOverviewUsers
    tasks: AdminOverviewTasks
    pending_submissions: int
    pending_redemptions: int
    today: AdminOverviewToday


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


class AdminInvitationRow(BaseModel):
    """One invitation on the §4.2 lifecycle list. ``email`` arrives
    MASKED from the service (G11: the plaintext rests on the row);
    ``status`` is derived at read time against the business clock."""

    model_config = ConfigDict(extra="forbid")

    id: str
    email: str
    role: Role
    status: Literal["PENDING", "ACCEPTED", "EXPIRED", "REVOKED"]
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None
    created_by: str
    issuer_nickname: str
    created_at: datetime


class AdminInvitationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AdminInvitationRow]


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
