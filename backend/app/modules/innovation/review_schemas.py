"""Versioned commands accept saved references only, never client snapshots."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
