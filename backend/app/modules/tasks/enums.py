# backend/app/modules/tasks/enums.py
"""Task module enums frozen by docs/architecture/interfaces.md.

Members and values are canonical (`value == member name`); database columns
persist the exact string as VARCHAR + CHECK constraints (see models.py for
why they are not PostgreSQL native enums).
"""

from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    PAUSED = "PAUSED"
    CLOSED = "CLOSED"
    ARCHIVED = "ARCHIVED"


class DeadlineMode(StrEnum):
    FIXED = "FIXED"
    RELATIVE = "RELATIVE"


class AssignmentAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    OCCUPIED = "OCCUPIED"
    COMPLETED = "COMPLETED"
    RETIRED = "RETIRED"


class ClaimStatus(StrEnum):
    CLAIMED = "CLAIMED"
    VALIDATING = "VALIDATING"
    UNDER_REVIEW = "UNDER_REVIEW"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"
    EXPIRED = "EXPIRED"


class TaskType(StrEnum):
    DATA_CRAWL = "DATA_CRAWL"


class TaskRarity(StrEnum):
    NORMAL = "NORMAL"
    RARE = "RARE"
    EPIC = "EPIC"
    LEGENDARY = "LEGENDARY"


class RewardLockStatus(StrEnum):
    """Frozen by interfaces.md (spec §11.2).

    It lives in this module because `AssignmentClaim.reward_lock_status`
    needs it from Plan 03 on; the Plan 04 submission module imports it from
    here instead of redefining the frozen member set.
    """

    NONE = "NONE"
    PROVISIONAL = "PROVISIONAL"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"
