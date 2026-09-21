# backend/app/modules/audit/service.py
"""The append-only writer for the durable audit log (G12; PR #2
hardening P0-5).

``AuditLogWriter.append`` is the ONLY code path that inserts an
``audit_logs`` row, and it is FLUSH-ONLY: the row joins the caller's
current transaction and the CALLER commits it (backend-engineering §5
transaction ownership — the ``LedgerService.post_entry`` discipline).
That is what makes "business write + audit row" one unit: the decision
and its trace commit together or not at all, with no second
transaction to lose and no outbox consumer to lag.

The writer is stateless (no clock, no ports): ``created_at`` is the
database ``now()`` server default, so the audit timestamp shares the
database's clock with every other ``created_at`` in the transaction
instead of a Python-side instant.

Module boundaries: this module imports nothing from the domain modules
— the dependency arrow points IN (community/points/identity import the
writer), so audit can never be coupled back to a caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditLog
from app.modules.identity.events import Actor

__all__ = ["AuditLogWriter"]


class AuditLogWriter:
    """The one INSERT path into ``audit_logs`` (see the module
    docstring); constructing it is free, so every composition root and
    every service default can hold the same behavior."""

    async def append(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        action: str,
        target_type: str,
        target_id: str,
        reason: str | None = None,
        details: Mapping[str, Any] | None = None,
        before_snapshot: Mapping[str, Any] | None = None,
        after_snapshot: Mapping[str, Any] | None = None,
        ip_address: str | None = None,
        request_id: str | None = None,
    ) -> AuditLog:
        """Append one audit row for ``actor`` acting on ``target``,
        flush-only — the caller owns the commit.

        ``actor`` is the frozen identity-events shape: the
        server-resolved role is snapshotted onto the row, and
        ``actor_user_id`` stores a plain UUID with no FK (the row
        outlives the account). ``target_id`` is the target's identifier
        as a string; ``reason`` is the action's free-text why when its
        contract carries one; ``details``, when given, must be
        JSON-serializable (the DomainEvent payload rule).

        ``before_snapshot``/``after_snapshot`` (spec §30, 0016) are the
        target's state migration as REDACTED structured JSON — business
        facts only (statuses, amounts, visibility), never PII (G11:
        nickname/phone/email/student id are forbidden in snapshots).
        Both stay NULL for access-style actions that mutate nothing.
        ``ip_address``/``request_id`` come from the caller's
        ``AuditContext`` when one exists; NULL on non-HTTP callers.
        """
        row = AuditLog(
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action=action,
            target_type=target_type,
            target_id=target_id,
            reason=reason,
            details=dict(details) if details is not None else None,
            before_snapshot=(
                dict(before_snapshot) if before_snapshot is not None else None
            ),
            after_snapshot=(
                dict(after_snapshot) if after_snapshot is not None else None
            ),
            ip_address=ip_address,
            request_id=request_id,
        )
        db.add(row)
        await db.flush()  # the caller commits (backend-engineering §5)
        return row
