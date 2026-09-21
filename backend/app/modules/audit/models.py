# backend/app/modules/audit/models.py
"""The durable audit log (quality-gates G12; PR #2 hardening P0-5).

Design decisions:

- **V1 writes audit rows DIRECTLY, in the caller's business transaction**
  (controller ruling on the audit-wave brief): the sensitive operation and
  its audit record commit or roll back together — one transaction, no
  event-consumer pipeline. The DomainEvent/NotificationPort streams are
  unchanged; a decoupled pipeline may arrive with Plan 08's query/UI work,
  which is also when read-side indexes get their own migration.
- **Append-only (G7/G12 discipline).** The table has no UPDATE and no
  DELETE path anywhere in the codebase: the only writer is
  ``AuditLogWriter.append`` (INSERT-only), no service exposes a mutation,
  and code review rejects any new one. Like ``points_ledger`` (models.py
  ruling), the database deliberately does not enforce this — no trigger,
  no rule — immutability is the service convention plus review
  discipline, and the table accordingly declares no ``updated_at`` and no
  onupdate column.
- **``actor_user_id`` carries NO foreign key on purpose:** an audit row
  must survive the deletion of the user it names (the fact "user X did
  Y" outlives the account), the same survival argument the polymorphic
  ``points_ledger.source_*`` pair makes. ``actor_role`` is the
  server-resolved role SNAPSHOT at action time — plain VARCHAR, not a
  CHECK against today's role vocabulary, because frozen history must not
  break when the role set grows.
- **``target_*`` is polymorphic by design:** ``target_type`` names the
  kind of thing acted on ("comment", "reward_redemption") and
  ``target_id`` its identifier as a string — a UUID rendered as text or a
  business key — so one table audits every surface without per-surface
  FK columns (the ledger ``source_type``/``source_id`` precedent).
- **``details`` is JSONB and optional:** structured context the action's
  contract wants beside the free-text ``reason`` (e.g. the reveal's
  ``revealed_user_id``, the redemption's ``user_id``/``reward_item_id``);
  values must be JSON-serializable (the DomainEvent payload rule).
- **§30's snapshot pair and request correlation (0016):**
  ``before_snapshot``/``after_snapshot`` carry the target's STATE
  MIGRATION as redacted structured JSON — statuses, amounts,
  visibility facts (G11: never nickname/phone/email/student id), so a
  reviewer answers "what changed" without joining the live row; both
  are NULL for access-style actions that mutate nothing (the reveal).
  ``ip_address``/``request_id`` are the request-scoped correlation
  pair threaded from the router through ``AuditContext`` — NULL on
  non-HTTP callers (workers, service-level tests), which is the
  documented default, not a gap. History rows written before 0016
  keep NULL snapshots: audit rows are append-only, so no backfill
  ever rewrites frozen decisions (the 0014 ruling).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

__all__ = ["AuditLog"]


class AuditLog(Base):
    """One durable record that a sensitive action happened (G12:
    actor/target/reason/timestamp).

    Append-only: no UPDATE or DELETE path exists (see the module
    docstring for the G7/G12 discipline); corrections are new rows, never
    edits of old ones.
    """

    __tablename__ = "audit_logs"

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    # Deliberately NOT a foreign key: the audit row outlives the user
    # (see the module docstring).
    actor_user_id: Mapped[UUID] = mapped_column()
    # Server-resolved role at action time, snapshotted (STUDENT/TEACHER/
    # ADMIN today; plain VARCHAR so frozen history never breaks).
    actor_role: Mapped[str] = mapped_column(String(16))
    # What the actor did, e.g. COMMUNITY_IDENTITY_REVEAL,
    # REDEMPTION_APPROVE (the identity-module audit-name family).
    action: Mapped[str] = mapped_column(String(64))
    # Polymorphic target ("comment", "reward_redemption", ...).
    target_type: Mapped[str] = mapped_column(String(32))
    # The target's id as a string (UUID text or business key).
    target_id: Mapped[str] = mapped_column(String(64))
    # Free-text WHY, when the action's contract carries one (mandatory
    # for the reveal and the redemption reject).
    reason: Mapped[str | None] = mapped_column(Text)
    # Structured context (JSON-serializable), optional.
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # §30: the target's state migration as redacted structured JSON
    # (G11: business facts only — never nickname/phone/email/student
    # id). NULL when the action mutates nothing (access-style audit).
    before_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Request correlation (AuditContext): the trusted client address and
    # the propagated request id. NULL on non-HTTP callers.
    ip_address: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
