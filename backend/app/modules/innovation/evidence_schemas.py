"""Explicit private DTOs; storage addresses never enter evidence reads."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EvidenceIntentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    content_type: Literal["application/pdf", "image/png", "image/jpeg"]
    size: int = Field(ge=1, le=10485760)


class EvidenceResponse(BaseModel):
    id: UUID
    content_type: str
    size: int
    state: Literal["PENDING", "CHECKING", "READY", "REJECTED"]
    sha256: str | None
    failure_code: str | None
    version: int
    created_at: datetime


class EvidenceIntentResponse(BaseModel):
    evidence: EvidenceResponse
    upload_url: str
    expires_at: datetime
    client_headers: dict[str, str]
    pinned_content_length: int


class EvidenceListResponse(BaseModel):
    items: list[EvidenceResponse]
