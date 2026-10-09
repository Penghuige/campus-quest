"""Versioned commands accept saved references only, never client snapshots."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Version = Annotated[int, Field(strict=True, ge=1)]


class SavedRevisionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    workflow_version: Version
    project_version: Version
    achievement_version: Version
    evidence_ids: list[UUID] = Field(min_length=1, max_length=5)

    @field_validator("evidence_ids")
    @classmethod
    def unique_materials(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("证明材料不能重复")
        return sorted(value)


class WithdrawCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_version: Version
    case_id: UUID
    case_version: Version


class ReviewCaseSummary(BaseModel):
    id: UUID
    revision_id: UUID
    status: Literal["SUBMITTED", "WITHDRAWN", "APPROVED", "RETURNED"]
    version: int
    reason: str | None


class AchievementWorkflowResponse(BaseModel):
    achievement_id: UUID
    first_review_state: Literal["DRAFT", "SUBMITTED", "RETURNED", "APPROVED"]
    moderation_state: Literal["NORMAL", "TAKEN_DOWN"]
    version: int
    review_case: ReviewCaseSummary | None
    public_revision_id: UUID | None
    first_approved_at: datetime | None
    latest_update_at: datetime | None
    has_unpublished_changes: bool


class ReviewVersionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Version


class ReviewDecisionCommand(ReviewVersionCommand):
    request_id: UUID
    revision_id: UUID
    decision: Literal["APPROVED", "RETURNED"]
    reason: str = Field(default="", max_length=1000)

    @field_validator("reason")
    @classmethod
    def trim_reason(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def require_return_reason(self) -> "ReviewDecisionCommand":
        if self.decision == "RETURNED" and not self.reason:
            raise ValueError("退回成果必须说明原因")
        return self


class ReviewQueueItem(ReviewCaseSummary):
    project_title: str
    achievement_title: str
    submitted_at: datetime
    claimed_by_me: bool


class ReviewQueueResponse(BaseModel):
    items: list[ReviewQueueItem]
    total: int
    limit: int
    offset: int


class ReviewPrivateDetail(BaseModel):
    case: ReviewCaseSummary
    project_content: dict[str, str]
    achievement_content: dict[str, str]
    owner_profile: dict[str, str]
    evidence: list[UUID]


class PublicProjectContent(BaseModel):
    title: str
    summary: str
    direction: str
    stage: str
    team_status: str


class PublicAchievementContent(BaseModel):
    title: str
    description: str
    work_url: str
    award_text: str


class PublicAchievementResponse(BaseModel):
    id: UUID
    project: PublicProjectContent
    achievement: PublicAchievementContent
    first_approved_at: datetime
    updated_at: datetime
    updated_after_first_review: bool


class PublicAchievementListResponse(BaseModel):
    items: list[PublicAchievementResponse]
    total: int
    limit: int
    offset: int
