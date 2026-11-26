# backend/app/modules/points/admin_wire_schemas.py
"""Pydantic wire schemas for the Admin reward/points/notification API."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.notifications.enums import NotificationChannel, NotificationEventType


def _reject_naive_datetime(value: datetime | None) -> datetime | None:
    """Refuse naive datetimes at the transport (G14): the columns are
    timezone-aware, so a naive value would fail deep inside the write
    instead of as a typed 422."""
    if value is not None and value.tzinfo is None:
        raise BusinessError(
            ErrorCode.VALIDATION_ERROR,
            "时间字段必须携带时区（timezone-aware）",
            status_code=422,
            details={"field": "available_from/available_until"},
        )
    return value


class AdminRewardItemResponse(BaseModel):
    """One catalogue row, admin view: every business field including
    the enabled flag and the fulfillment instructions the student
    listing omits."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str | None
    point_cost: int
    stock: int | None
    per_user_term_limit: int | None
    available_from: datetime | None
    available_until: datetime | None
    enabled: bool
    requires_manual_review: bool
    fulfillment_instructions: str | None


class AdminRewardItemListResponse(BaseModel):
    """Offset-paginated catalogue page, name-ordered, disabled rows
    INCLUDED — the management view (the student listing's enabled-only
    read is the student surface)."""

    model_config = ConfigDict(extra="forbid")

    items: list[AdminRewardItemResponse]
    total: int
    limit: int
    offset: int


class RewardItemCreateRequest(BaseModel):
    """The catalogue row to create (starts ENABLED — taking an item off
    the shelf is the dedicated disable transition)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    point_cost: int
    description: str | None = None
    stock: int | None = None
    per_user_term_limit: int | None = None
    available_from: datetime | None = None
    available_until: datetime | None = None
    requires_manual_review: bool = False
    fulfillment_instructions: str | None = None
    reason: str | None = None

    @field_validator("available_from", "available_until")
    @classmethod
    def _require_tz_aware(cls, value: datetime | None) -> datetime | None:
        return _reject_naive_datetime(value)


class RewardItemUpdateRequest(BaseModel):
    """A partial catalogue update: an ABSENT field leaves the column
    unchanged; an explicit null clears a nullable bound. The route
    translates presence into the service's UNSET discipline."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    point_cost: int | None = None
    description: str | None = None
    stock: int | None = None
    per_user_term_limit: int | None = None
    available_from: datetime | None = None
    available_until: datetime | None = None
    requires_manual_review: bool | None = None
    fulfillment_instructions: str | None = None
    reason: str | None = None

    @field_validator("available_from", "available_until")
    @classmethod
    def _require_tz_aware(cls, value: datetime | None) -> datetime | None:
        return _reject_naive_datetime(value)


class RewardItemDisableRequest(BaseModel):
    """下架 one item — the dedicated, reason-requiring transition."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1)


class RewardReviewGrantRequest(BaseModel):
    """Grant the global REWARD_REVIEW authorization to a Teacher; the
    mandatory why rides the audited grant row."""

    model_config = ConfigDict(extra="forbid")

    teacher_id: UUID
    reason: str = Field(min_length=1)


class RewardReviewGrantResponse(BaseModel):
    """One live grant row: the teacher (id + display nickname through
    the frozen directory port) plus the grant facts. The grant's
    who/why HISTORY lives in the audit stream, not the row."""

    model_config = ConfigDict(extra="forbid")

    teacher_id: str
    nickname: str | None
    granted_by: str
    granted_at: datetime


class RewardReviewGrantListResponse(BaseModel):
    """Offset-paginated grants page, newest grant first."""

    model_config = ConfigDict(extra="forbid")

    items: list[RewardReviewGrantResponse]
    total: int
    limit: int
    offset: int


class PointsAdjustmentRequest(BaseModel):
    """One manual wallet correction through the ledger (never a direct
    wallet write): a non-zero amount, the mandatory reason, and the
    caller-minted ``operation_id`` — the adjustment's stable intent id
    (PR #5 fix A, P1). A retry after a lost response reuses the SAME
    id and replays the original entry instead of double-charging; the
    same id under a different decision is the typed 409."""

    model_config = ConfigDict(extra="forbid")

    amount: int
    reason: str = Field(min_length=1)
    operation_id: UUID


class PointsAdjustmentResponse(BaseModel):
    """The posted ADMIN_ADJUSTMENT entry: ranking-neutral by
    construction (affects_ranking=false; no ranking-affecting
    adjustment operation exists)."""

    model_config = ConfigDict(extra="forbid")

    ledger_entry_id: str
    user_id: str
    amount: int
    reason: str


class NotificationTemplateCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: NotificationEventType
    channel: NotificationChannel
    title: str = Field(min_length=1)
    template_body: str = Field(min_length=1)


class NotificationTemplateUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    template_body: str = Field(min_length=1)


class AdminNotificationTemplateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    event_type: str
    channel: str
    title: str
    template_body: str
    enabled: bool
    version: int


class AdminNotificationTemplateListResponse(BaseModel):
    """Offset-paginated template page, (event_type, channel)-ordered,
    disabled rows INCLUDED — the administration view."""

    model_config = ConfigDict(extra="forbid")

    items: list[AdminNotificationTemplateResponse]
    total: int
    limit: int
    offset: int
