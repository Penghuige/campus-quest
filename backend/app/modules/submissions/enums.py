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
"""

from __future__ import annotations

from enum import StrEnum

from app.modules.tasks.enums import RewardLockStatus

__all__ = ["RewardLockStatus", "ReviewAction", "ReviewStatus", "ValidationStatus"]


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

    Module-internal vocabulary (not frozen by interfaces.md):
    `INVALIDATE_LOCK` is the database-stable spelling of the spec's
    INVALIDATE_REWARD_LOCK review outcome.
    """

    APPROVE = "APPROVE"
    REQUIRE_REVISION = "REQUIRE_REVISION"
    INVALIDATE_LOCK = "INVALIDATE_LOCK"
