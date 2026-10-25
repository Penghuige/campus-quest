"""Talent upload signing is private; file metadata never contains object keys."""

import unicodedata
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

TalentFileType = Literal["application/pdf", "image/png", "image/jpeg"]


class TalentFileIntentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    name: str
    content_type: TalentFileType
    size: Annotated[int, Field(strict=True, ge=1, le=10485760)]

    @field_validator("name")
    @classmethod
    def display_name(cls, value: str) -> str:
        value = value.replace("\\", "/").rsplit("/", 1)[-1]
        value = "".join(c for c in value if unicodedata.category(c) != "Cc").strip()
        if not value or value in {".", ".."} or len(value) > 200:
            raise ValueError("文件名称须为 1–200 字，不含路径")
        return value


class TalentFileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    name: str
    content_type: TalentFileType
    size: int
    state: Literal["PENDING", "CHECKING", "READY", "REJECTED"]
    failure_code: str | None
    version: int
    removed_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class TalentFileIntentResponse(BaseModel):
    file: TalentFileResponse
    upload_url: str
    expires_at: datetime
    client_headers: dict[str, str]
    pinned_content_length: int


class TalentFileListResponse(BaseModel):
    items: list[TalentFileResponse]
