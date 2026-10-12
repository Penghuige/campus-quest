"""Private talent editor DTOs; completeness is checked only on publication."""

from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.modules.innovation.recruitment_schemas import ContactChannel, ContactInput

CooperationState = Literal["LOOKING", "OPEN_TO_OPPORTUNITIES", "NOT_AVAILABLE"]
WorkMode = Literal["UNSPECIFIED", "ONLINE", "OFFLINE", "EITHER"]
TalentContact = ContactInput


class TalentLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ]
    url: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2048)]

    @field_validator("url")
    @classmethod
    def safe_url(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            valid = parsed.scheme in {"http", "https"} and parsed.hostname is not None
            _ = parsed.port
        except ValueError:
            valid = False
        if not valid or any(ord(char) <= 32 for char in value):
            raise ValueError("作品链接须为完整 http/https 地址")
        return value


class TalentFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)] = ""
    faculty: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=100)
    ] = ""
    grade: Annotated[str, StringConstraints(strip_whitespace=True, max_length=30)] = ""
    introduction: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=1000)
    ] = ""
    advantages: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=2000)
    ] = ""
    tag_ids: list[UUID] = Field(default_factory=list, max_length=10)
    skill_notes: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=1000)
    ] = ""
    cooperation_direction: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=500)
    ] = ""
    desired_roles: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=500)
    ] = ""
    weekly_hours: Annotated[int, Field(strict=True, ge=0, le=168)] | None = None
    work_mode: WorkMode = "UNSPECIFIED"
    cooperation_state: CooperationState | None = None
    experience: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=3000)
    ] = ""
    links: list[TalentLink] = Field(default_factory=list, max_length=5)
    file_ids: list[UUID] = Field(default_factory=list, max_length=5)
    contacts: list[ContactInput] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def unique_selections(self) -> "TalentFields":
        if len(set(self.tag_ids)) != len(self.tag_ids) or len(
            set(self.file_ids)
        ) != len(self.file_ids):
            raise ValueError("标签和作品不能重复选择")
        channels = [contact.channel for contact in self.contacts]
        if len(set(channels)) != len(channels):
            raise ValueError("每种联系渠道只能授权一次")
        return self

    def validate_publication(self) -> None:
        required = (
            self.name,
            self.faculty,
            self.grade,
            self.introduction,
            self.advantages,
        )
        if not all(required) or not self.tag_ids or self.cooperation_state is None:
            raise ValueError(
                "请完善姓名、院系、年级、介绍、优势、至少一个技能标签及合作状态"
            )


class TalentSaveCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    version: Annotated[int, Field(strict=True, ge=0)]
    fields: TalentFields


class TalentVersionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    version: Annotated[int, Field(strict=True, ge=1)]


class TalentRevokeContactCommand(TalentVersionCommand):
    channel: ContactChannel


class TalentFileCommand(TalentVersionCommand):
    pass


class TalentProfileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    fields: TalentFields
    version: int
    visibility: Literal["HIDDEN", "VISIBLE"]
    public_version: int
    public_contact_channels: list[ContactChannel]
    public_file_ids: list[UUID]
    has_unpublished_changes: bool
    updated_at: datetime


class TalentProfileReadResponse(BaseModel):
    profile: TalentProfileResponse | None


class TalentActionResult(BaseModel):
    action: str
    replayed: bool
    applied_version: int
    current: TalentProfileResponse


class TalentTagResponse(BaseModel):
    id: UUID
    name: str


class TalentTagListResponse(BaseModel):
    items: list[TalentTagResponse]


class TalentSuggestionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)
    ]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = (
        ""
    )


class TalentSuggestionResponse(BaseModel):
    id: UUID
    status: Literal["PENDING"]
    replayed: bool
