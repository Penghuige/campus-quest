# backend/app/modules/identity/models.py
"""Identity persistence models (spec §5, §35; invariants §31.1-31.2).

Design decisions:

- Enum-like columns (`role`, `status`) are VARCHAR with explicitly named
  CHECK constraints, not PostgreSQL native enums: adding a member later is a
  constraint swap instead of an `ALTER TYPE`, and the CHECK still keeps
  invalid values out at the database boundary (backend-engineering §8). The
  short constraint names compose with the naming convention in
  `app.db.base` into `ck_users_role` / `ck_users_status` /
  `ck_staff_invitations_role`.
- `username` (student number) is VARCHAR and is never converted to an
  integer: leading zeros must survive (spec §5.2). The 64-character bound is
  an unbounded-input guard only; the configurable 6-20 digit rule for
  students is application validation (`validation.py`), as is the whitelist
  hit.
- `phone_e164` is nullable with a partial unique index
  (`WHERE phone_e164 IS NOT NULL`): a STUDENT sits in PENDING_PHONE before
  the OTP flow binds a phone, and staff accounts may carry no phone at all,
  while every bound phone stays globally unique (spec §5.4, §31.2). "an
  ACTIVE student must have a phone" is a service rule; the column cannot be
  NOT NULL without breaking the PENDING_PHONE state.
- `email_normalized` is nullable with a partial unique index: V1 uses global
  uniqueness for non-null normalized emails, and unbound (NULL) emails never
  collide (spec §5.5).
- Refresh rotation detection: `user_sessions.replaced_by` points at the
  successor session, so a presented refresh token whose session was already
  replaced is detected as reuse without a denormalized flag or timestamp
  guess (spec §5.6).
- 2FA state lives in `totp_credentials` only (one row per user;
  `confirmed_at IS NOT NULL` means enabled). There is deliberately no
  `two_factor_enabled` column on `users`: two sources of truth could drift.
- `display_honor_id` (migration 0008) points at the rankings module's
  `honors` table: the one honor the user chose to display (spec §18). It
  is a plain nullable pointer — ownership (a matching `user_honors` row)
  is `rankings.honor_service.set_display_honor`'s rule, not a composite
  FK — and identity never imports the rankings models: the display
  title is resolved through a typed Core light table in `directory.py`
  (the tasks module's `_USERS_LOCK` seam, mirrored).
- No ORM relationships are declared yet; navigation joins arrive with the
  services that need them (backend-engineering §8).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    """Account row shared by students and staff (spec §5.2-5.7)."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('STUDENT', 'TEACHER', 'ADMIN')",
            name="role",
        ),
        CheckConstraint(
            "status IN ('PENDING_PHONE', 'ACTIVE', 'SUSPENDED', 'BANNED')",
            name="status",
        ),
        Index(
            "uq_users_phone_e164",
            "phone_e164",
            unique=True,
            postgresql_where=text("phone_e164 IS NOT NULL"),
        ),
        Index(
            "uq_users_email_normalized",
            "email_normalized",
            unique=True,
            postgresql_where=text("email_normalized IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    nickname: Mapped[str] = mapped_column(String(255))
    phone_e164: Mapped[str | None] = mapped_column(String(32))
    email_normalized: Mapped[str | None] = mapped_column(String(320))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    role: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    # The user's chosen display honor (spec §18; the honors table is
    # rankings-owned, migration 0008). Plain nullable pointer; ownership
    # is set_display_honor's service rule — see the module docstring.
    display_honor_id: Mapped[UUID | None] = mapped_column(ForeignKey("honors.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=func.now()
    )


class StudentWhitelist(Base):
    """Pre-imported undergraduate student numbers (spec §5.1).

    Whitelisting a number never creates a User, and disabling an entry never
    touches an existing account; both are service-level rules.
    """

    __tablename__ = "student_whitelist"

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    student_number: Mapped[str] = mapped_column(String(64), unique=True)
    # "metadata" is reserved by Declarative; the column keeps the spec name.
    meta: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserSession(Base):
    """Server-side refresh session; one row per issued refresh token (§5.6)."""

    __tablename__ = "user_sessions"

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    refresh_token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Session that superseded this one at rotation time; NOT NULL on a
    # presented token means the token was already rotated (reuse signal).
    replaced_by: Mapped[UUID | None] = mapped_column(ForeignKey("user_sessions.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class StaffInvitation(Base):
    """One-shot, short-lived staff invitation (spec §5.8)."""

    __tablename__ = "staff_invitations"
    __table_args__ = (
        CheckConstraint(
            "role IN ('TEACHER', 'ADMIN')",
            name="role",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    email_normalized: Mapped[str] = mapped_column(String(320), index=True)
    role: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class TotpCredential(Base):
    """Encrypted TOTP secret for mandatory staff 2FA (spec §5.6, §5.8).

    The primary key on `user_id` also enforces one credential per user.
    `confirmed_at IS NULL` marks a started-but-unconfirmed setup.
    """

    __tablename__ = "totp_credentials"

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    secret_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class RecoveryCode(Base):
    """Hashed one-use recovery code, shown once when 2FA is enabled (§5.8)."""

    __tablename__ = "recovery_codes"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "code_hash", name="uq_recovery_codes_user_id_code_hash"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String(128))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
