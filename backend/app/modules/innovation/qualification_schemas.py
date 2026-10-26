"""Qualification state omits PII; audited admin detail reveals only the snapshot."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class QualificationApply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Annotated[int, Field(strict=True, ge=0)]
    profile_version: Annotated[int, Field(strict=True, ge=1)]


class QualificationApprove(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Annotated[int, Field(strict=True, ge=1)]


class QualificationState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["NOT_APPLIED", "PENDING", "APPROVED"]
    version: int
    profile_version: int | None
    requested_at: datetime | None
    approved_at: datetime | None


class QualificationProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    student_no: str
    major: str
    grade: str


class QualificationDetail(QualificationState):
    user_id: UUID
    profile: QualificationProfile


class QualificationQueueItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: UUID
    version: int
    requested_at: datetime


class QualificationQueue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[QualificationQueueItem]
    total: int
