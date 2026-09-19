# backend/app/modules/identity/events.py
"""Domain primitives and the audit-event seam (interfaces.md).

`Actor` and `DomainEvent` are the frozen "Core Primitives" shapes from
docs/architecture/interfaces.md, verbatim. They land here (Plan 02, Task 6)
because staff onboarding needs both before Task 7's `dependencies.py`
exists; Task 7 re-exports `Actor` as the authenticated request context
instead of redefining it, so there is exactly one Actor type.

The audit action names below are audit-stream identifiers consumed by Plan
08's `AuditService` (spec §5.8: 账号创建、角色提升、角色撤销必须写 AuditLog).
They are deliberately NOT additions to the §25 notification event list in
interfaces.md — that list is the notification-delivery contract; this is
the audit contract. Persistence arrives with Plan 08; until then the
service publishes through `DomainEventPublisher`, whose in-memory
implementation (`InMemoryEventCollector`) is what tests (and Plan 08's
outbox wiring) attach to.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.modules.identity.enums import Role

logger = logging.getLogger(__name__)

# Audit action names (see module docstring for the audit-vs-notification
# distinction). Emitted by `staff_service.StaffService`.
STAFF_INVITATION_CREATED = "STAFF_INVITATION_CREATED"
STAFF_INVITATION_ACCEPTED = "STAFF_INVITATION_ACCEPTED"
TOTP_ENABLED = "TOTP_ENABLED"


@dataclass(frozen=True, slots=True)
class Actor:
    """The authenticated context passed to service calls (interfaces.md).

    ``role`` is the server-resolved role at request time, never a
    client-supplied value (backend-engineering §16).
    """

    user_id: UUID
    role: Role


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """One immutable business/audit occurrence (interfaces.md).

    ``occurred_at`` is UTC-aware; ``payload`` must be JSON-serializable so
    Plan 08 can persist it without a second projection.
    """

    event_type: str
    aggregate_type: str
    aggregate_id: UUID
    occurred_at: datetime
    payload: Mapping[str, Any] = field(default_factory=dict)


class DomainEventPublisher(Protocol):
    """Port through which services emit domain/audit events.

    The production adapter (Plan 08 `AuditService` / outbox) attaches inside
    the domain transaction; the in-memory collector below is the test and
    interim-production adapter.
    """

    def publish(self, event: DomainEvent) -> None: ...


class InMemoryEventCollector:
    """Deterministic `DomainEventPublisher` fake: records, delivers nothing.

    Tests assert on ``events`` (ordered); Plan 08 swaps this for the
    persistent audit write without touching the publishing services.
    """

    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    def publish(self, event: DomainEvent) -> None:
        self.events.append(event)

    def of_type(self, event_type: str) -> list[DomainEvent]:
        return [event for event in self.events if event.event_type == event_type]


class LoggingEventPublisher:
    """Interim production adapter: log the event, persist nothing.

    Plan 08's `AuditService`/outbox replaces this at the composition root.
    Only the event type and aggregate identity are logged — the payload
    carries PII (emails, roles) and stays out of application logs
    (backend-engineering §15).
    """

    def publish(self, event: DomainEvent) -> None:
        logger.info(
            "domain event type=%s aggregate_type=%s aggregate_id=%s",
            event.event_type,
            event.aggregate_type,
            event.aggregate_id,
        )
