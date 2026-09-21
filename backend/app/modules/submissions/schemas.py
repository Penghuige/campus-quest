# backend/app/modules/submissions/schemas.py
"""Submission module transport contract: upload intent and finalize
request/response models plus the public Submission DTO (spec §10, §11,
§28, §33.3, §40; backend-engineering §9, §16).

Single responsibility: the submission/review wire shapes, and nothing else.

- The ``*Request`` models are the untrusted transport input. Pydantic
  enforces only the structural bounds (non-empty capped filename, the
  closed FileType universe, a non-negative size); every business rule —
  the Task's allowed set, the size caps, the window — belongs to
  ``UploadService``/``UploadPolicyService``, the single validation
  authority. The one deliberate exception: the review note and the
  invalidation reason are MANDATORY at the transport (spec §11.3's
  reviewer-facing requirement) — the strip-and-reject validator below is
  a shape rule, not a business decision.
- The ``*Response`` models enumerate their fields and are built
  explicitly (``from_domain``/``from_view``/field-by-field), never
  serialized from an ORM object. ``SubmissionPublic`` therefore cannot
  carry ``object_key`` (spec §40: 对象存储原始路径 must never leak to
  clients) nor any internal/audit column: privacy by construction.
- ``UploadIntentResponse`` hands the client exactly what the presigned
  flow needs: the intent id to finalize later, the short-lived upload
  URL, and when that URL dies. The server-generated object key stays
  server-side.
- ``SubmissionValidationResponse``/``ValidationReportPayload`` are the
  student-safe §12.4 projection: the persisted report travels as-is
  (it contains no object keys and no parser internals by construction —
  see ``validators.common``'s wire-format note) plus the status
  projections. ``report`` is ``None`` while the run has not finished.
- ``DownloadUrlResponse`` carries the short-lived presigned URL and its
  expiry (spec §33.3) — the key itself never appears as a field; the
  URL is the only storage material, and it is minted per request AFTER
  the authorization check.
- The teacher review shapes (``ReviewQueue*``, the three action
  responses) are explicitly-authorized DTOs: they may carry the review
  context (platform/keyword, locked tier) the student surface never
  sees, and still no object keys — the queue's ``download_url`` is the
  API path that mints the presigned link on demand.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.modules.submissions.enums import FileType
from app.modules.submissions.models import Submission


def _require_non_blank(value: str) -> str:
    """Shape rule shared by the review note and the invalidation reason:
    strip, then refuse blanks (spec §11.3's mandatory reviewer text)."""
    stripped = value.strip()
    if not stripped:
        raise ValueError("不能为空白")
    return stripped


class UploadIntentRequest(BaseModel):
    """Request an upload intent (spec §10 step 1): the claim being
    submitted against, the display filename, the declared file type, and
    the declared byte size the backend checks against the Task's file
    policy before issuing a presigned URL."""

    claim_id: UUID
    filename: str = Field(min_length=1, max_length=255)
    declared_type: FileType
    size: int = Field(ge=0)


class UploadIntentResponse(BaseModel):
    """The issued grant: the intent id for the later finalize call, the
    short-lived presigned upload URL, its expiry instant, and the exact
    PUT headers the URL signed — the client echoes them verbatim on the
    PUT (write-once condition, pinned content type, declared byte
    length); reconstructing them from documentation is a contract gap
    the composition smoke surfaced (PR #2 hardening step 13)."""

    intent_id: UUID
    upload_url: str
    expires_at: datetime
    headers: dict[str, str]


class UploadCompleteRequest(BaseModel):
    """Notify the backend that the presigned PUT landed (spec §10 step 5).

    The intent id travels in the BODY: the URL shape stays the spec §28
    ``POST /submissions/upload-complete`` (the resource being completed
    is the single-use grant, not a Submission yet — there is no
    submission id to put in the path).
    """

    intent_id: UUID


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


class ValidationFindingPayload(BaseModel):
    """One bounded §12.4 finding sample (error or warning)."""

    code: str
    message: str
    row: int | None = None
    column: str | None = None
    value: str | None = None


class ValidationReportPayload(BaseModel):
    """The student/reviewer-safe §12.4 report, exactly as persisted.

    Field-by-field over the stored JSONB (the ``report_to_json`` shape):
    counts stay exact, finding/preview lists are already bounded by the
    report builder, and no object key or parser internal exists in the
    source shape to leak.
    """

    parser_version: str
    file_type: str
    row_count: int
    detected_columns: list[str]
    missing_required_columns: list[str]
    extra_columns: list[str]
    type_error_counts: dict[str, int]
    null_ratios: dict[str, float]
    duplicate_counts: dict[str, int]
    warnings: list[ValidationFindingPayload]
    errors: list[ValidationFindingPayload]
    duration_ms: float
    preview_rows: list[list[str]]

    @classmethod
    def from_persisted(cls, data: Mapping[str, Any]) -> ValidationReportPayload:
        """Validate-and-copy the persisted JSONB into the wire model."""
        return cls(
            parser_version=data["parser_version"],
            file_type=data["file_type"],
            row_count=data["row_count"],
            detected_columns=list(data["detected_columns"]),
            missing_required_columns=list(data["missing_required_columns"]),
            extra_columns=list(data["extra_columns"]),
            type_error_counts=dict(data["type_error_counts"]),
            null_ratios=dict(data["null_ratios"]),
            duplicate_counts=dict(data["duplicate_counts"]),
            warnings=[ValidationFindingPayload(**item) for item in data["warnings"]],
            errors=[ValidationFindingPayload(**item) for item in data["errors"]],
            duration_ms=data["duration_ms"],
            preview_rows=[list(row) for row in data.get("preview_rows", ())],
        )


class SubmissionValidationResponse(BaseModel):
    """The owner's view of one submission's machine-validation outcome
    (spec §11.1, §12.4): status projections plus the persisted report —
    ``None`` while the run has not reached a terminal state."""

    submission_id: UUID
    claim_id: UUID
    version: int
    validation_status: str
    review_status: str
    detected_type: str | None
    report: ValidationReportPayload | None


class DownloadUrlResponse(BaseModel):
    """A short-lived presigned download grant (spec §33.3), minted per
    request after the authorization check. The response carries the URL
    and its expiry — never the object key."""

    url: str
    expires_at: datetime


class ReviewQueueItemResponse(BaseModel):
    """One review-queue row (spec §41/§28): the VALIDATED submission
    with the claim/task context the reviewer needs to judge it. The
    ``download_url`` is the API path that mints the short-lived
    presigned link on demand (authorized per request), not a presigned
    URL that would expire inside a listed page."""

    submission_id: UUID
    claim_id: UUID
    task_id: UUID
    task_title: str
    platform: str
    keyword: str
    version: int
    original_filename: str
    declared_type: str
    detected_type: str | None
    file_size: int
    submitted_at: datetime
    review_status: str
    claim_status: str
    reward_tier_locked: int | None
    locked_reward_points: int | None
    validation: ValidationReportPayload | None
    download_url: str


class ReviewQueueResponse(BaseModel):
    """One offset page of the teacher review queue."""

    items: list[ReviewQueueItemResponse]
    total: int
    limit: int
    offset: int


class RevisionRequiredRequest(BaseModel):
    """Return the submission for fixes (spec §11.3). The note — the
    teacher's guidance — is mandatory at the transport."""

    note: str = Field(min_length=1, max_length=2000)

    @field_validator("note")
    @classmethod
    def _note_not_blank(cls, value: str) -> str:
        return _require_non_blank(value)


class InvalidateRewardLockRequest(BaseModel):
    """Cancel the provisional reward lock (spec §11.3). The reviewer
    reason is mandatory — it lands on the append-only audit rows."""

    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        return _require_non_blank(value)


class ApproveResponse(BaseModel):
    """The §14 approve outcome: the claim's landing state and the single
    grant. ``already_reviewed`` is True only on the idempotent replay
    (nothing written, no second grant)."""

    claim_id: UUID
    claim_status: str
    reward_lock_status: str
    points_granted: int | None
    already_reviewed: bool


class RevisionRequiredResponse(BaseModel):
    """The claim state after 退回/判无效: REVISION_REQUIRED with the §11.4
    revision deadline. ``reward_lock_status`` distinguishes the two —
    preserved PROVISIONAL versus cancelled INVALIDATED."""

    claim_id: UUID
    claim_status: str
    reward_lock_status: str
    revision_deadline_at: datetime


#: Shared by revision-required and invalidate-reward-lock (same shape).
InvalidationResponse = RevisionRequiredResponse
