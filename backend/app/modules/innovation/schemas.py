"""Explicit owner-management DTOs; no persistence/private identity leakage."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Title = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
]
Summary = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]
Direction = Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)]
Stage = Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)]
TeamStatus = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]
Version = Annotated[int, Field(strict=True, ge=1)]


class ProjectDraftCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    title: Title
    summary: Summary = ""
    direction: Direction = ""
    stage: Stage = ""
    team_status: TeamStatus = ""


class ProjectDraftUpdate(BaseModel):
    """PATCH submits the complete five-field editor plus the observed version."""

    model_config = ConfigDict(extra="forbid")

    title: Title
    summary: Summary
    direction: Direction
    stage: Stage
    team_status: TeamStatus
    version: Version


class ProjectDraftResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    title: str
    summary: str
    direction: str
    stage: str
    team_status: str
    version: int
    created_at: datetime
    updated_at: datetime


class ProjectDraftListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ProjectDraftResponse]
    total: int
    limit: int
    offset: int
