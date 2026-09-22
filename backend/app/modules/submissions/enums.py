# backend/app/modules/submissions/enums.py
"""Submission module enums frozen by docs/architecture/interfaces.md.

Members and values are canonical (`value == member name`); database columns
persist the exact string as VARCHAR + CHECK constraints (see models.py for
why they are not PostgreSQL native enums).

`RewardLockStatus` is frozen in the tasks module because
`AssignmentClaim.reward_lock_status` needs it; it is re-exported here so
submission-side services import the whole lock vocabulary from one place
without redefining the frozen member set (same precedent as identity
re-exports).

`FileType` and `RetentionPolicy` are the submission module's own closed
universes: the CSV/XLSX/SQLITE file types (spec §10/§12 — the same
member set the tasks module's `allowed_file_types` CHECK guards) and the
retention policies (spec §13 — the same values the Task column's CHECK
guards). They live here, not in the tasks module, because the submission
side computes against them (declared-type MIME pinning, retention
snapshots) while the tasks side persists raw strings; the member sets
must stay in lockstep with those CHECK constraints.
"""

from __future__ import annotations

from enum import StrEnum

from app.modules.tasks.enums import RewardLockStatus

__all__ = [
    "FileType",
    "RetentionPolicy",
    "RewardLockStatus",
    "ReviewAction",
    "ReviewStatus",
    "ValidationStatus",
]


class FileType(StrEnum):
    """The closed upload file-type universe (spec §10/§12).

    Mirrors the `tasks.allowed_file_types` CHECK member set; a Task may
    restrict to a subset but never beyond it.
    """

    CSV = "CSV"
    XLSX = "XLSX"
    SQLITE = "SQLITE"


class RetentionPolicy(StrEnum):
    """Raw-upload retention policies (spec §13): 30/90/180 days or an
    explicit permanent flag.

    Mirrors the `tasks.retention_policy` CHECK member set; the upload
    finalize service snapshots the Task's current policy into every
    Submission at finalize time.
    """

    DAYS_30 = "DAYS_30"
    DAYS_90 = "DAYS_90"
    DAYS_180 = "DAYS_180"
    PERMANENT = "PERMANENT"


class ValidationStatus(StrEnum):
    """Machine validation stage on Submission (spec §11.1)."""

    UPLOADED = "UPLOADED"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    VALIDATION_FAILED = "VALIDATION_FAILED"


class ReviewStatus(StrEnum):
    """Human review stage on Submission (spec §11.1).

    `PENDING_REVIEW` is the pre-review initial value fixed by
    interfaces.md; `APPROVED` implies Claim COMPLETED (spec §14).
    """

    PENDING_REVIEW = "PENDING_REVIEW"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    REVISION_REQUIRED = "REVISION_REQUIRED"


class ReviewAction(StrEnum):
    """Immutable review decision recorded per review (spec §11.3).

    Frozen by interfaces.md ("ReviewAction"): `INVALIDATE_LOCK` is the
    database-stable spelling of the spec's INVALIDATE_REWARD_LOCK review
    outcome, and `SubmissionReview.action` persists the exact string.
    """

    APPROVE = "APPROVE"
    REQUIRE_REVISION = "REQUIRE_REVISION"
    INVALIDATE_LOCK = "INVALIDATE_LOCK"
