"""Four self-reported private fields; never an authorization command."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class OwnerProfileSave(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)
    ]
    student_no: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)
    ]
    major: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
    ]
    grade: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)
    ]
    version: Annotated[int, Field(strict=True, ge=0)]


class OwnerProfileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    student_no: str
    major: str
    grade: str
    version: int
    created_at: datetime
    updated_at: datetime


class OwnerProfileReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: OwnerProfileResponse | None
