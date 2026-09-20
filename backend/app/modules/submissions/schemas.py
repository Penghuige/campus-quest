# backend/app/modules/submissions/schemas.py
"""Submission module transport contract: upload intent and finalize
request/response models plus the public Submission DTO (spec §10, §11,
§40; backend-engineering §9, §16).

Single responsibility: the upload flow's wire shapes, and nothing else.

- The ``*Request`` models are the untrusted transport input. Pydantic
  enforces only the structural bounds (non-empty capped filename, the
  closed FileType universe, a non-negative size); every business rule —
  the Task's allowed set, the size caps, the window — belongs to
  ``UploadService``/``UploadPolicyService``, the single validation
  authority.
- The ``*Response`` models enumerate their fields and are built
  explicitly (``from_domain``/field-by-field), never serialized from an
  ORM object. ``SubmissionPublic`` therefore cannot carry ``object_key``
  (spec §40: 对象存储原始路径 must never leak to clients) nor any
  internal/audit column: privacy by construction. Teacher-facing review
  shapes that legitimately need more arrive with the review routes and
  get their own explicitly-authorized DTOs.
- ``UploadIntentResponse`` hands the client exactly what the presigned
  flow needs: the intent id to finalize later, the short-lived upload
  URL, and when that URL dies. The server-generated object key stays
  server-side.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.modules.submissions.enums import FileType
from app.modules.submissions.models import Submission


class UploadIntentRequest(BaseModel):
    """Request an upload intent (spec §10 step 1): the display filename,
    the declared file type, and the declared byte size the backend checks
    against the Task's file policy before issuing a presigned URL."""

    filename: str = Field(min_length=1, max_length=255)
    declared_type: FileType
    size: int = Field(ge=0)


class UploadIntentResponse(BaseModel):
    """The issued grant: the intent id for the later finalize call, the
    short-lived presigned upload URL, and its expiry instant."""

    intent_id: UUID
    upload_url: str
    expires_at: datetime


class SubmissionPublic(BaseModel):
    """Privacy-safe Submission read DTO (spec §11, §40).

    Deliberately minimal: no ``object_key`` (downloads go through
    short-lived signed URLs issued by an authorized route), no reviewer
    identity, no retention/legal-hold internals. The validation and
    review status values are the frozen enum strings.
    """

    id: UUID
    claim_id: UUID
    version: int
    original_filename: str
    declared_type: str
    file_size: int
    submitted_at: datetime
    validation_status: str
    review_status: str
    created_at: datetime

    @classmethod
    def from_domain(cls, submission: Submission) -> SubmissionPublic:
        """Build the DTO explicitly from a Submission row; nothing else
        leaks."""
        return cls(
            id=submission.id,
            claim_id=submission.claim_id,
            version=submission.version,
            original_filename=submission.original_filename,
            declared_type=submission.declared_type,
            file_size=submission.file_size,
            submitted_at=submission.submitted_at,
            validation_status=submission.validation_status,
            review_status=submission.review_status,
            created_at=submission.created_at,
        )


class FinalizeResponse(BaseModel):
    """The finalize outcome: the created (or replayed, spec §32)
    submission in its public shape."""

    submission: SubmissionPublic
