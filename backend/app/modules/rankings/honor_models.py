# backend/app/modules/rankings/honor_models.py
"""Honor persistence models (spec §18; plan 05 task 7).

Design decisions:

- Honors are separate from the points asset (spec §18: 荣誉与积分资产
  分离): nothing in this module writes ``points_ledger`` or touches the
  Redis ranking projection. An honor grant can never mutate ranking
  points, including Admin commemorative honors.
- Enum-like ``honor_type`` is VARCHAR with an explicit named CHECK, not
  a native enum (same rationale as every other module). The domain is
  the five frozen first-version AUTO types plus ``COMMEMORATIVE`` for
  Admin-created纪念 honors (spec §18: "Admin 可以人工创建纪念
  Honor"): ``is_auto = false`` marks exactly those rows, and the
  ``period_required`` coherence CHECK keeps the two families apart.
- ``period`` is REQUIRED exactly for the periodic types
  DAILY_RANK/MONTHLY_RANK (spec §18: 周期性荣誉必须带 period，例如
  2026-09 月度第一) and FORBIDDEN otherwise — the biconditional CHECK
  makes a timeless honor carrying a period unrepresentable. Daily
  periods are ``YYYY-MM-DD`` (10 chars), monthly ``YYYY-MM`` (7), so
  VARCHAR(16) bounds both.
- ONE HONOR ROW PER PERIOD for periodic types: ``UserHonor`` is
  UNIQUE(user_id, honor_id), so re-granting "the" monthly honor every
  month would collide; instead each period gets its own Honor row (the
  2026-09 月度 Top 3 is a different row from the 2026-10 one). Rows are
  created lazily by ``honor_service.evaluate_honors`` — the fixed-rule
  DEFINITIONS live in code (``FIXED_HONOR_DEFINITIONS``), the table
  holds only definitions somebody has actually earned.
- Auto definition identity is ``(honor_type, name, period)`` — the plan
  drafted UNIQUE(honor_type, period) for periodic rows, but MONTHLY_RANK
  carries TWO definitions per period (rank 1 卷王 and rank<=3 Top 3, the
  spec §18 example list), so ``name`` must discriminate. Two partial
  unique indexes over ``is_auto`` rows enforce it: the periodic one
  where ``period IS NOT NULL``, the lifetime one where ``period IS
  NULL`` (a plain UNIQUE would treat every NULL as distinct).
- ``UserHonor`` is append-only grant history: UNIQUE(user_id, honor_id)
  is the idempotency key a replayed evaluation converges onto (spec
  §32-style retry semantics; the service also pre-checks so the common
  replay never even attempts the INSERT). Honors are never revoked in
  V1 — an ON_TIME_STREAK honor earned once stays earned even after the
  streak later breaks.
- ``users.display_honor_id`` (added by migration 0008 on the identity
  side) is the single display honor a user may choose (spec §18: 只能
  设置一个 display_honor_id). Ownership (a matching UserHonor row) is a
  service rule — ``set_display_honor`` — not a composite FK, so the
  column stays a plain nullable pointer.
- No ORM relationships are declared; navigation joins arrive with the
  services that need them (backend-engineering §8).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

__all__ = ["Honor", "HonorType", "UserHonor"]


class HonorType(StrEnum):
    """The honor_type vocabulary (spec §18 + the Admin commemorative row).

    The first five members are the frozen first-version AUTO honor
    types; ``COMMEMORATIVE`` marks Admin-created纪念 honors
    (``Honor.is_auto = false``).
    """

    TOTAL_COMPLETED = "TOTAL_COMPLETED"
    ON_TIME_STREAK = "ON_TIME_STREAK"
    DAILY_RANK = "DAILY_RANK"
    MONTHLY_RANK = "MONTHLY_RANK"
    TOTAL_EARNED_POINTS = "TOTAL_EARNED_POINTS"
    COMMEMORATIVE = "COMMEMORATIVE"


# The periodic honor types (spec §18: must carry a period).
PERIODIC_HONOR_TYPES: Final[frozenset[str]] = frozenset(
    {HonorType.DAILY_RANK.value, HonorType.MONTHLY_RANK.value}
)


class Honor(Base):
    """One honor definition row (spec §18).

    Auto rows are lazily created instances of the code-owned fixed-rule
    catalog (one row per definition, per period for the periodic
    types); ``is_auto = false`` rows are Admin commemorative honors.
    """

    __tablename__ = "honors"
    __table_args__ = (
        CheckConstraint(
            "honor_type IN "
            "("
            "'TOTAL_COMPLETED', "
            "'ON_TIME_STREAK', "
            "'DAILY_RANK', "
            "'MONTHLY_RANK', "
            "'TOTAL_EARNED_POINTS', "
            "'COMMEMORATIVE'"
            ")",
            name="honor_type",
        ),
        # Periodic types MUST carry a period and nothing else MAY: the
        # biconditional makes both halves unrepresentable the wrong way
        # (spec §18).
        CheckConstraint(
            "(honor_type IN ('DAILY_RANK', 'MONTHLY_RANK')) = (period IS NOT NULL)",
            name="period_required",
        ),
        # Auto definition identity (see module docstring): the periodic
        # arm includes `name` because MONTHLY_RANK has two definitions
        # per period. Admin rows (is_auto = false) are unconstrained —
        # two commemorative honors may share a name.
        Index(
            "uq_honors_periodic_definition",
            "honor_type",
            "name",
            "period",
            unique=True,
            postgresql_where=text("period IS NOT NULL AND is_auto"),
        ),
        Index(
            "uq_honors_lifetime_definition",
            "honor_type",
            "name",
            unique=True,
            postgresql_where=text("period IS NULL AND is_auto"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    honor_type: Mapped[str] = mapped_column(String(24))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    # YYYY-MM-DD (DAILY_RANK) or YYYY-MM (MONTHLY_RANK); NULL exactly
    # for the lifetime types and commemorative rows.
    period: Mapped[str | None] = mapped_column(String(16))
    # True for fixed-rule auto rows; False for Admin commemorative rows.
    is_auto: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class UserHonor(Base):
    """One honor grant (spec §18): a user owns a Honor from
    ``granted_at`` on, forever in V1.

    UNIQUE(user_id, honor_id) is the grant idempotency key — the
    database-side anchor a replayed evaluation converges onto. Append-only
    by service convention (no UPDATE path, no onupdate column); the
    database deliberately does not enforce it, same ruling as the audit
    tables.
    """

    __tablename__ = "user_honors"
    __table_args__ = (
        UniqueConstraint("user_id", "honor_id", name="uq_user_honors_user_id_honor_id"),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    honor_id: Mapped[UUID] = mapped_column(ForeignKey("honors.id"), index=True)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
