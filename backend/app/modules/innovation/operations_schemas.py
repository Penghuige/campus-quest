"""Versioned scoped authority, with no identity-profile fields."""

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class OperationsGrantSave(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: Annotated[bool, Field(strict=True)]
    version: Annotated[int, Field(strict=True, ge=0)]
    reason: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ]


class OperationsGrantResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    enabled: bool
    version: int


class InnovationCapabilitiesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operations_enabled: bool
