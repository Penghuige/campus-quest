"""Private editor DTOs and explicit, contact-free public recruitment shapes."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.modules.innovation.contact_schemas import ContactChannel, ContactInput
from app.modules.innovation.review_schemas import PublicAchievementListResponse
from app.modules.innovation.schemas import Version

RecruitmentStatus = Literal["DRAFT", "OPEN", "CLOSED", "EXPIRED"]
Text120 = Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)]
Text2000 = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]


class RecruitmentFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_title: Text120 = ""
    direction: Text120 = ""
    stage: Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)] = ""
    introduction: Text2000 = ""
    idea_stage: StrictBool | None = None
    goal: Text2000 = ""
    initial_plan: Text2000 = ""
    role_name: Text120 = ""
    headcount: Annotated[int, Field(strict=True, ge=1, le=999)] | None = None
    duties: Text2000 = ""
    requirements: Text2000 = ""
    time_commitment: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=500)
    ] = ""
    beginner_friendly: StrictBool | None = None
    deadline_at: AwareDatetime | None = None
    contacts: list[ContactInput] = Field(default_factory=list, max_length=4)

    @field_validator("deadline_at")
    @classmethod
    def utc_deadline(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def unique_channels(self) -> "RecruitmentFields":
        if len({item.channel for item in self.contacts}) != len(self.contacts):
            raise ValueError("每种联系渠道只能选一个")
        return self

    def validate_publication(self, now: datetime) -> None:
        required = (
            "project_title",
            "direction",
            "stage",
            "introduction",
            "role_name",
            "duties",
            "requirements",
            "time_commitment",
        )
        if any(not getattr(self, name) for name in required):
            raise ValueError("请填全项目介绍和岗位信息")
        if (
            self.idea_stage is None
            or self.beginner_friendly is None
            or self.headcount is None
        ):
            raise ValueError("请明确选择创意起步、零基础和人数")
        if self.idea_stage and (not self.goal or not self.initial_plan):
            raise ValueError("创意起步须填写目标与初步方案")
        if not self.contacts:
            raise ValueError("请明确选择至少一种授权联系渠道")
        if self.deadline_at is not None and self.deadline_at <= now:
            raise ValueError("截止时间必须在未来")


class RecruitmentDraftCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    fields: RecruitmentFields


class RecruitmentVersionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Version
    request_id: UUID


class RecruitmentDraftUpdate(RecruitmentVersionCommand):
    fields: RecruitmentFields


class RevokeRecruitmentContactCommand(RecruitmentVersionCommand):
    channel: ContactChannel
    close_if_last: StrictBool = False


class RecruitmentDraftResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    project_id: UUID
    fields: RecruitmentFields
    version: int
    status: RecruitmentStatus
    moderation_state: Literal["NORMAL", "TAKEN_DOWN"]
    public_contact_channels: list[ContactChannel]
    has_unpublished_changes: bool
    cycle_id: UUID | None
    deadline_at: datetime | None
    last_confirmed_at: datetime | None
    next_remind_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RecruitmentDraftListResponse(BaseModel):
    items: list[RecruitmentDraftResponse]
    total: int
    limit: int
    offset: int


class RecruitmentActionResult(BaseModel):
    action: str
    replayed: bool
    applied_version: int
    current: RecruitmentDraftResponse


class PublicRecruitmentContent(BaseModel):
    """No contact values or private persistence fields are representable."""

    model_config = ConfigDict(extra="forbid")
    project_title: str
    direction: str
    stage: str
    introduction: str
    idea_stage: bool
    goal: str
    initial_plan: str
    role_name: str
    headcount: int
    duties: str
    requirements: str
    time_commitment: str
    beginner_friendly: bool
    deadline_at: datetime | None


class PublicRecruitmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    project_id: UUID
    revision_number: int
    content: PublicRecruitmentContent
    contact_channels: list[ContactChannel]
    published_at: datetime


class PublicRecruitmentListResponse(BaseModel):
    items: list[PublicRecruitmentResponse]
    total: int
    limit: int
    offset: int


class RecruitmentContactsResponse(BaseModel):
    id: UUID
    revision_number: int
    contacts: list[ContactInput]


class PublicProjectFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: Annotated[str, Field(max_length=120)] = ""
    direction: Annotated[str, Field(max_length=120)] = ""
    stage: Annotated[str, Field(max_length=80)] = ""
    recruiting_only: bool = False
    beginner_friendly: bool | None = None
    limit: Annotated[int, Field(ge=1, le=50)] = 20
    offset: Annotated[int, Field(ge=0)] = 0


class PublicProjectSummary(BaseModel):
    id: UUID
    title: str
    summary: str
    direction: str
    stage: str
    source_kind: Literal["ACHIEVEMENT", "RECRUITMENT"]
    achievement_count: int
    recruitment_count: int


class PublicProjectListResponse(BaseModel):
    items: list[PublicProjectSummary]
    total: int
    limit: int
    offset: int


class PublicProjectDetailResponse(BaseModel):
    project: PublicProjectSummary
    achievements: PublicAchievementListResponse
    recruitments: PublicRecruitmentListResponse
