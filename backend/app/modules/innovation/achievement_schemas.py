"""Private preparation content, no review or publication authority."""

from datetime import datetime
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
)

_URL = TypeAdapter(AnyHttpUrl)


class AchievementDraftContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
    ]
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=4000)
    ] = ""
    work_url: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=2000)
    ] = ""
    award_text: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=1000)
    ] = ""

    @field_validator("work_url")
    @classmethod
    def valid_work_url(cls, value: str) -> str:
        if not value:
            return value
        raw = urlsplit(value)
        if raw.scheme.lower() not in {"http", "https"} or not raw.netloc:
            raise ValueError("请填写完整的 http 或 https 作品链接")
        if any(ord(char) <= 32 or ord(char) == 127 or char == "\\" for char in value):
            raise ValueError("作品链接不能包含空白、控制字符或反斜杠")
        parsed = _URL.validate_python(value)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("作品链接不能包含账号密码")
        # Keep the publisher's text; validation never fetches the URL.
        return value


class AchievementDraftCreate(AchievementDraftContent):
    request_id: UUID


class AchievementDraftUpdate(AchievementDraftContent):
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=4000)
    ]
    work_url: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]
    award_text: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=1000)
    ]
    version: Annotated[int, Field(strict=True, ge=1)]


class AchievementDraftResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    title: str
    description: str
    work_url: str
    award_text: str
    version: int
    created_at: datetime
    updated_at: datetime


class AchievementDraftListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AchievementDraftResponse]
    total: int
    limit: int
    offset: int
