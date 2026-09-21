# backend/app/modules/system/service.py
"""The system-settings service: audited writes and plain reads of the
platform's current-value configuration (PR #2 hardening step 8).

The service is GENERIC over keys — a setting's value semantics (the
academic term's ≤64 shape) belong to the transport schema that owns
the key (the admin settings router), not to this storage layer. What
the service does own:

- **``set`` writes the value and its audit row in ONE transaction**
  (backend-engineering §5): the write path resolves first-write races
  INSIDE the database (``INSERT ... ON CONFLICT DO NOTHING RETURNING``;
  the loser then locks the winner's row ``FOR UPDATE`` — see ``set``),
  the flush-only ``AuditLogWriter.append`` joins the caller's
  transaction, and the service commits exactly once — value and trace
  commit or roll back together, the RedemptionService discipline. The
  audit row carries the value MIGRATION on the §30 snapshot pair
  (0016): the NEW value in ``after_snapshot.value`` and the PREVIOUS
  value in ``before_snapshot.value`` (``None`` on the first write), so
  the audit trail answers "what was it before?" without a time
  machine — and the chain stays CONNECTED under a lost first-write
  race (round-5 P1): the loser reads the winner's committed value
  under the row lock, never a pre-read stale ``None``.
- **Keys and values are stored STRIPPED and never blank**: whitespace
  is not configuration state (the term-key semantics the
  ``AcademicTermProvider`` family applies at read time). A blank-after-
  strip key or value is the typed §29 ``VALIDATION_ERROR`` (422), not
  a 500. Length is the transport schema's call (the term key's 64 is
  the ``RewardRedemption.term_key`` column width); the service bounds
  only the key at its own column width.
- **``get`` is a read of the current value only**: ``None`` means "no
  row" — the caller decides the fallback (the academic-term provider
  falls back to the deployment seed; G7). Read-only, commits nothing.

Module boundaries: like ``audit``, this module imports nothing from
the domain modules — the arrow points IN (points' composition reads
the setting; the admin router writes it).
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.events import Actor
from app.modules.system.models import SystemSetting

__all__ = [
    "CURRENT_ACADEMIC_TERM",
    "SYSTEM_SETTING_UPDATED",
    "SystemSettingService",
    "SystemSettingValueError",
]

# Audit action name (the STAFF_INVITATION_CREATED family; G12 durable
# audit): one row per applied setting write, inside the write
# transaction.
SYSTEM_SETTING_UPDATED = "SYSTEM_SETTING_UPDATED"

# The audit target vocabulary for setting writes: the setting's key IS
# the target id (a business key, not a UUID — the audit_logs polymorphic
# target design).
_AUDIT_TARGET_TYPE = "system_setting"

# SystemSetting.key column width (models.py): a longer key is a caller
# bug, not a truncation candidate.
_KEY_MAX_LENGTH = 64

# The setting keys the platform knows (plan 08's full surface grows
# this family here). CURRENT_ACADEMIC_TERM is the term key snapshotted
# onto new reward redemptions (spec §16.1): read per request by points'
# SystemAcademicTermProvider, written through the admin settings API.
# Both import the name from this module so the storage address is
# spelled once.
CURRENT_ACADEMIC_TERM = "CURRENT_ACADEMIC_TERM"


class SystemSettingValueError(BusinessError):
    """A blank (after strip) or over-width key — the request's shape is
    wrong, so this is the §29 VALIDATION_ERROR business code (422),
    never a 500: the caller sent an unusable payload."""

    def __init__(self, field: str, value: str) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            "系统设置的键或值不能为空白",
            status_code=422,
            details={"field": field, "value": value},
        )


def _validated(field: str, raw: str, max_length: int | None = None) -> str:
    """The stripped value, or the typed §29 validation error.

    ``max_length`` bounds the value at a column width when one applies
    (the key); values are unbounded TEXT — their shape is the transport
    schema's contract.
    """
    stripped = raw.strip() if isinstance(raw, str) else ""
    if not stripped or (max_length is not None and len(stripped) > max_length):
        raise SystemSettingValueError(field, raw)
    return stripped


class SystemSettingService:
    """Reads and audited writes of the ``system_settings`` current-value
    store (see the module docstring).

    ``audit`` defaults to a fresh ``AuditLogWriter`` — stateless and
    flush-only; the default means the default writer, never "no
    auditing" (the RedemptionService wiring ruling: a wiring slip must
    not silently drop audit rows).
    """

    def __init__(self, audit: AuditLogWriter | None = None) -> None:
        self._audit = audit if audit is not None else AuditLogWriter()

    async def get(self, db: AsyncSession, key: str) -> str | None:
        """The key's current value, or ``None`` when no row exists (the
        caller's fallback decides — see the module docstring)."""
        return cast(
            "str | None",
            await db.scalar(
                select(SystemSetting.value).where(SystemSetting.key == key)
            ),
        )

    async def set(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        key: str,
        value: str,
        audit_context: AuditContext | None = None,
    ) -> str:
        """Store ``value`` as the key's current value and write the
        ``SYSTEM_SETTING_UPDATED`` audit row in the SAME transaction —
        conflict-decided insert or locked update, flush, audit, one
        commit (§5; see the write-path comment for the race pattern).

        Returns the stored (stripped) value. The value MIGRATION rides
        the §30 snapshot pair (0016): ``before_snapshot={"value": ...}``
        is the previous value (``None`` on the first write),
        ``after_snapshot={"value": ...}`` the stored one — configuration
        facts, no PII (G11). The ``before`` is TRUE under a lost
        first-write race: the loser reads the winner's committed value
        under the row lock, so the audit chain stays connected."""
        stored_key = _validated("key", key, _KEY_MAX_LENGTH)
        stored_value = _validated("value", value)
        # Chain-true first write (owner round-5 P1): the race decides
        # INSIDE the database. INSERT ... ON CONFLICT DO NOTHING
        # RETURNING — the winner (a row returned) created the key in
        # THIS transaction, so previous is None by construction. The
        # loser (no row returned: a concurrent transaction committed
        # this key while this one waited on the conflict) then locks
        # the winner's committed row FOR UPDATE and reads the TRUE
        # previous before updating, so the audited chain stays
        # connected (None→X, X→Y) even under a lost first-write race.
        # The pass-4 UPSERT shape pre-read the old value and audited a
        # stale None→Y for the loser — the owner ruled that
        # unacceptable: under concurrency the audit migration must not
        # be false (quality-gates §16/G12).
        inserted = await db.execute(
            pg_insert(SystemSetting)
            .values(
                key=stored_key,
                value=stored_value,
                updated_by_user_id=actor.user_id,
            )
            .on_conflict_do_nothing(index_elements=[SystemSetting.key])
            .returning(SystemSetting.key)
        )
        previous: str | None
        if inserted.first() is not None:
            previous = None  # this transaction created the key
        else:
            row = await db.scalar(
                select(SystemSetting)
                .where(SystemSetting.key == stored_key)
                .with_for_update()
                # Fresh values under the row lock even if this
                # session's identity map already holds the instance
                # (the redemption service's locked-select discipline).
                .execution_options(populate_existing=True)
            )
            # The conflict-proven row cannot vanish under the lock: the
            # DO NOTHING insert returned nothing only because another
            # transaction COMMITTED this key, and settings have no
            # delete path.
            assert row is not None
            previous = row.value
            row.value = stored_value
            row.updated_by_user_id = actor.user_id
        await db.flush()
        await self._audit.append(
            db,
            actor=actor,
            action=SYSTEM_SETTING_UPDATED,
            target_type=_AUDIT_TARGET_TYPE,
            target_id=stored_key,
            before_snapshot={"value": previous},
            after_snapshot={"value": stored_value},
            ip_address=audit_context.ip_address if audit_context else None,
            request_id=audit_context.request_id if audit_context else None,
        )
        await db.commit()  # value + audit row: one unit (§5)
        return stored_value
