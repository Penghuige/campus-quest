# backend/app/modules/system/models.py
"""The system-settings current-value store (PR #2 hardening step 8;
plan 08's full system-settings surface seeds here).

Design decisions:

- **This table is a CURRENT-VALUE store, NOT history — UPDATE is the
  normal write path.** Each known key holds exactly one row with the
  value the platform should use NOW; changing a setting UPDATES that
  row. This is the deliberate opposite of ``audit_logs`` (0014), which
  is append-only because it IS history: there, corrections are new rows
  and the table forbids nothing only by review discipline. Here, the
  history of every change lives in ``audit_logs`` — one
  ``SYSTEM_SETTING_UPDATED`` row per applied write, committed in the
  same transaction by ``SystemSettingService.set`` — so the current
  value store does not need to accumulate rows to stay auditable. The
  database accordingly carries no trigger and no audit columns beyond
  ``updated_by_user_id``/``updated_at``; immutability would be wrong,
  not merely unenforced.
- **``updated_by_user_id`` carries NO foreign key on purpose** (the
  ``audit_logs.actor_user_id`` ruling): a setting row must record who
  last set it, and that attribution must survive the deletion of the
  actor's account — the fact "admin X set the term" outlives the user.
- **A missing row is a meaningful state**: it means "never configured
  here; use the deployment seed" (``Settings.current_academic_term``
  for the academic term — G7: the settings row is the fact, the env
  var is the initial seed). Migrations deliberately insert no default
  rows; the fallback belongs to the reader
  (``SystemAcademicTermProvider``), not to seeded data that would
  silently freeze the env value into the database.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

__all__ = ["SystemSetting"]


class SystemSetting(Base):
    """One setting's current value, keyed by name.

    Current-value semantics (see the module docstring): ``set`` paths
    UPDATE this row (or INSERT the first value for a new key) and write
    the change's audit row in the same transaction; the change history
    is in ``audit_logs``, never accumulated here.
    """

    __tablename__ = "system_settings"

    # The setting's name, e.g. CURRENT_ACADEMIC_TERM (a business key —
    # the key IS the identity, so no surrogate id column).
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    # Deliberately NOT a foreign key: the attribution survives the
    # actor's account deletion (the audit_logs actor ruling).
    updated_by_user_id: Mapped[UUID] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=func.now()
    )
