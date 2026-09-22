# backend/app/modules/community/enums.py
"""Community module enums (spec §20-23).

Members and values are canonical (`value == member name`); database columns
persist the exact string as VARCHAR + CHECK constraints (see models.py for
why they are not PostgreSQL native enums). `VoteValue` is the exception: it
mirrors the integer ±1 vote convention (spec §22) as an `IntEnum`, and its
column is INTEGER + CHECK.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class VoteValue(IntEnum):
    """Comment vote direction (spec §22): +1 like, -1 dislike.

    An IntEnum (not StrEnum) because `comment_votes.value` is an INTEGER
    column CHECK-constrained to exactly these two values; assigning a
    member sends its integer value to the driver.
    """

    UP = 1
    DOWN = -1


class ReportCategory(StrEnum):
    """Closed report-category set (spec §23)."""

    SPAM = "SPAM"
    HARASSMENT = "HARASSMENT"
    PRIVACY = "PRIVACY"
    OTHER = "OTHER"


class ReportStatus(StrEnum):
    """Moderation-queue lifecycle of a CommentReport (spec §23).

    Status set decision (the spec names only `status` + `handled_by`/
    `handled_at`): a report enters the queue as OPEN and leaves it exactly
    once, either HANDLED (moderation acted on the comment) or DISMISSED
    (no action). Both terminal transitions are performed by a moderator,
    so `handled_by`/`handled_at` are filled on either outcome; the choice
    between them is what distinguishes the action taken. No reopen
    transition exists in V1 — a user whose report was dismissed may still
    file a different category on the same comment (the UNIQUE triple
    permits that).
    """

    OPEN = "OPEN"
    HANDLED = "HANDLED"
    DISMISSED = "DISMISSED"
