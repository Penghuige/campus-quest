# backend/app/modules/notifications/enums.py
"""Notification module enums frozen by docs/architecture/interfaces.md.

Members and values are canonical (`value == member name`); database columns
persist the exact string as VARCHAR + CHECK constraints (see models.py for
why they are not PostgreSQL native enums). New event types require updating
interfaces.md first: the set is frozen, and the CHECK constraints on
notifications and notification_templates are generated from it.
"""

from __future__ import annotations

from enum import StrEnum


class NotificationChannel(StrEnum):
    """Delivery channels (spec §25; frozen by interfaces.md)."""

    SMS = "SMS"
    EMAIL = "EMAIL"
    IN_APP = "IN_APP"


class DeliveryStatus(StrEnum):
    """NotificationDelivery lifecycle (spec §25.3/§25.4; frozen by
    interfaces.md).

    PENDING is the initial due-scheduled state; RETRYABLE marks a failed
    attempt still inside the bounded retry policy; SENDING is the
    claim-before-send in-flight state; SENT and FAILED are terminal.
    """

    PENDING = "PENDING"
    RETRYABLE = "RETRYABLE"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"


#: Prefix of the last_error marker a POLICY skip records on a row it
#: resolves SENT (delivery_service, spec §25.1/§25.2 skips — not
#: failures): "skipped:<token>", e.g. "skipped:email_not_verified" or
#: "skipped:deadline_claim_status:UNDER_REVIEW". Lives beside the frozen
#: status set because it is part of the SENT semantics: the inbox
#: visibility gate (inbox_service) reads it back to tell a policy skip
#: from a real provider send on the same terminal status.
SKIPPED_LAST_ERROR_PREFIX = "skipped:"


class NotificationEventType(StrEnum):
    """Canonical domain event names (spec §25 list plus the
    SUBMISSION_VALIDATION_FAILED addition; frozen by interfaces.md)."""

    ASSIGNMENT_DEADLINE_24H = "ASSIGNMENT_DEADLINE_24H"
    ASSIGNMENT_DEADLINE_4H = "ASSIGNMENT_DEADLINE_4H"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    SUBMISSION_APPROVED = "SUBMISSION_APPROVED"
    SUBMISSION_VALIDATION_FAILED = "SUBMISSION_VALIDATION_FAILED"
    REWARD_REDEMPTION_APPROVED = "REWARD_REDEMPTION_APPROVED"
    REWARD_REDEMPTION_REJECTED = "REWARD_REDEMPTION_REJECTED"
    ACCOUNT_SECURITY = "ACCOUNT_SECURITY"
