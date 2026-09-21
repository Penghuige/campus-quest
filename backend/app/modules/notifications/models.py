# backend/app/modules/notifications/models.py
"""Notification module persistence models (spec §25).

Design decisions:

- Enum-like columns are VARCHAR with explicitly named CHECK constraints,
  not PostgreSQL native enums — same rationale as the identity and tasks
  modules (adding a member is a constraint swap, not ALTER TYPE). The
  short CHECK names compose with the naming convention in `app.db.base`
  into e.g. `ck_notification_deliveries_channel`; the IN-lists are
  generated from the frozen enums in this module so the Python member
  set and the database boundary cannot drift apart.
- `Notification` is the logical per-user message: exactly one row per
  (event_key, user_id). Channel fan-out lives in `NotificationDelivery`
  (spec §25: services create one logical Notification plus per-channel
  delivery records). Title and body are rendered snapshots taken when the
  event is recorded, so a later Admin template edit never rewrites an
  already-created notification — the same snapshot philosophy as the
  claim-time task contract snapshots in the tasks module.
- `UNIQUE(event_key, user_id, channel)` on deliveries is THE idempotency
  boundary (spec §25.3): a duplicate event, a Celery retry, or two
  workers racing to schedule the same send all collapse onto one row per
  channel. Event keys follow `<aggregate>:<id>:<suffix>`, e.g.
  `claim:123:deadline_4h` (interfaces.md).
- `user_id` is denormalized onto the delivery (the notification already
  carries it) so the due-delivery scan, admin failure queries, and the
  unique constraint itself never need a join first — same rationale as
  `task_id` on assignment_claims.
- `notification_id` is NOT NULL: deliveries are created in the same
  business transaction as their notification (event durability — the
  rows must commit with the domain state they describe), so an orphan
  channel-state row cannot exist.
- `attempts` is CHECK-pinned only to >= 0. The retry ceiling (spec §25.4:
  ~1m/~5m/~20m, at most 3 or a configured value) is runtime
  configuration, so the database must not freeze it.
- `status` defaults to PENDING; RETRYABLE/SENDING/SENT/FAILED complete
  the frozen lifecycle (interfaces.md). IN_APP deliveries are rows like
  any other channel: the notifications row itself is the visible message,
  and its IN_APP delivery row tracks dispatch state (spec §25.4: on
  permanent failure the in-app fallback remains).
- `scheduled_at` is NOT NULL timestamptz — every delivery is due at a
  definite instant — and (status, scheduled_at) carries a composite index
  for the due-delivery scan.
- `read_at` on the logical row backs the notification list and mark-read
  API (spec §28); read state is per message, not per channel.
- `NotificationTemplate` is UNIQUE(event_type, channel) with an integer
  `version` that Admin edits bump (spec §25.5); template rendering stays
  constrained in service code, not in the schema.
- No ORM relationships are declared yet; navigation joins arrive with the
  services that need them (backend-engineering §8).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.modules.notifications.enums import (
    DeliveryStatus,
    NotificationChannel,
    NotificationEventType,
)

_EVENT_TYPE_LIST = ", ".join(f"'{event.value}'" for event in NotificationEventType)
_CHANNEL_LIST = ", ".join(f"'{channel.value}'" for channel in NotificationChannel)
_DELIVERY_STATUS_LIST = ", ".join(f"'{status.value}'" for status in DeliveryStatus)


class Notification(Base):
    """One logical notification message for one user (spec §25)."""

    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(
            f"event_type IN ({_EVENT_TYPE_LIST})",
            name="event_type",
        ),
        UniqueConstraint(
            "event_key", "user_id", name="uq_notifications_event_key_user_id"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    event_key: Mapped[str] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(48))
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=func.now()
    )


class NotificationDelivery(Base):
    """Per-channel delivery state for one notification (spec §25.3).

    One row per (event_key, user_id, channel); retries and duplicate
    dispatches mutate this row's status/attempts instead of inserting.
    """

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        CheckConstraint(
            f"channel IN ({_CHANNEL_LIST})",
            name="channel",
        ),
        CheckConstraint(
            f"status IN ({_DELIVERY_STATUS_LIST})",
            name="status",
        ),
        CheckConstraint("attempts >= 0", name="attempts"),
        UniqueConstraint(
            "event_key",
            "user_id",
            "channel",
            name="uq_notification_deliveries_event_key_user_id_channel",
        ),
        Index(
            "ix_notification_deliveries_status_scheduled_at",
            "status",
            "scheduled_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    notification_id: Mapped[UUID] = mapped_column(
        ForeignKey("notifications.id"), index=True
    )
    # Denormalized from Notification (see module docstring).
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    event_key: Mapped[str] = mapped_column(String(255))
    channel: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'PENDING'"))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    # NO onupdate here (unlike the other updated_at columns): this
    # column is the V1 SENDING-lease timestamp and must stay in the
    # SERVICE clock domain only. tx2 finalize re-assigns the same
    # service instant tx1 claimed with; with an ORM onupdate present
    # SQLAlchemy prunes the net-unchanged column from the UPDATE and
    # the DB clock silently overwrites the lease (mixed time domains;
    # plan 07 final review I1). No DDL delta: onupdate never reached
    # the migration.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class NotificationTemplate(Base):
    """Admin-editable message template per event type and channel
    (spec §25.5)."""

    __tablename__ = "notification_templates"
    __table_args__ = (
        CheckConstraint(
            f"event_type IN ({_EVENT_TYPE_LIST})",
            name="event_type",
        ),
        CheckConstraint(
            f"channel IN ({_CHANNEL_LIST})",
            name="channel",
        ),
        CheckConstraint("version >= 1", name="version"),
        UniqueConstraint(
            "event_type", "channel", name="uq_notification_templates_event_type_channel"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    event_type: Mapped[str] = mapped_column(String(48))
    channel: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(255))
    template_body: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=func.now()
    )
