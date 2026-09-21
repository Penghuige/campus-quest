# backend/app/modules/system/service.py
"""The system-settings service: audited writes and plain reads of the
platform's current-value configuration (PR #2 hardening step 8).

The service is GENERIC over keys — a setting's value semantics (the
academic term's ≤64 shape) belong to the transport schema that owns
the key (the admin settings router), not to this storage layer. What
the service does own:

- **``set`` writes the value and its audit row in ONE transaction**
  (backend-engineering §5): the row lock (FOR UPDATE) serializes
  concurrent writes of the same key, the flush-only
  ``AuditLogWriter.append`` joins the caller's transaction, and the
  service commits exactly once — value and trace commit or roll back
  together, the RedemptionService discipline. The audit row carries
  the NEW value in ``details.value`` and the PREVIOUS value in
  ``details.old_value`` (``None`` on the first write), so the audit
  trail answers "what was it before?" without a time machine.
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

One known narrow race, documented rather than papered over: two
concurrent ``set`` calls for a brand-NEW key can both miss the row and
both INSERT; the loser fails the primary key at flush (500
INTERNAL_ERROR, that transaction writes nothing) and its retry finds
the row. Concurrent writes to an EXISTING key — the real admin
workflow — serialize on the row lock, so ``old_value`` in the audit
row is exact.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
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
    ) -> str:
        """Store ``value`` as the key's current value and write the
        ``SYSTEM_SETTING_UPDATED`` audit row in the SAME transaction —
        row lock, insert-or-update, flush, audit, one commit (§5).

        Returns the stored (stripped) value. The audit ``details``
        carry ``value`` (the new value) and ``old_value`` (the previous
        value, ``None`` on the first write).
        """
        stored_key = _validated("key", key, _KEY_MAX_LENGTH)
        stored_value = _validated("value", value)
        # First write upsert (PR #2 closure review P2): two concurrent
        # writes of a brand-new key both miss the FOR UPDATE select and
        # both INSERT — the pk loser surfaced IntegrityError as a 500.
        # The insert path is now a PostgreSQL UPSERT: the constraint
        # conflict resolves inside the database, so a concurrent admin
        # write is serialization, never an INTERNAL_ERROR. The
        # pre-read still supplies the audited old_value (a lost race
        # may audit a stale old_value; the row itself stays correct —
        # last write wins under the upsert, and a settings key has one
        # authoritative writer in practice).
        row = await db.scalar(
            select(SystemSetting)
            .where(SystemSetting.key == stored_key)
            .with_for_update()
            # Fresh values under the row lock even if this session's
            # identity map already holds the instance (the redemption
            # service's locked-select discipline).
            .execution_options(populate_existing=True)
        )
        previous = row.value if row is not None else None
        if row is None:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            statement = (
                pg_insert(SystemSetting)
                .values(
                    key=stored_key,
                    value=stored_value,
                    updated_by_user_id=actor.user_id,
                )
                .on_conflict_do_update(
                    index_elements=[SystemSetting.key],
                    set_={
                        "value": stored_value,
                        "updated_by_user_id": actor.user_id,
                        # The ORM-level onupdate does not fire for this
                        # core upsert — stamp the touch explicitly.
                        "updated_at": func.now(),
                    },
                )
            )
            await db.execute(statement)
        else:
            row.value = stored_value
            row.updated_by_user_id = actor.user_id
        await db.flush()
        await self._audit.append(
            db,
            actor=actor,
            action=SYSTEM_SETTING_UPDATED,
            target_type=_AUDIT_TARGET_TYPE,
            target_id=stored_key,
            details={"value": stored_value, "old_value": previous},
        )
        await db.commit()  # value + audit row: one unit (§5)
        return stored_value
