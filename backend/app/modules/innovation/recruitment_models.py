"""Recruitment drafts, immutable content revisions and revocable authorizations."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_TEXT_LIMITS = {
    "project_title": 120,
    "direction": 120,
    "stage": 80,
    "introduction": 2000,
    "goal": 2000,
    "initial_plan": 2000,
    "role_name": 120,
    "duties": 2000,
    "requirements": 2000,
    "time_commitment": 500,
}


def _text_bounds(column: str) -> tuple[CheckConstraint, ...]:
    return tuple(
        CheckConstraint(
            f"{column} ? '{key}' AND jsonb_typeof({column}->'{key}') = 'string' "
            f"AND char_length({column}->>'{key}') <= {limit}",
            name=f"{key}_length",
        )
        for key, limit in _TEXT_LIMITS.items()
    )


class RecruitmentDraft(Base):
    __tablename__ = "ie_recruitment_drafts"
    __table_args__ = (
        *_text_bounds("fields"),
        UniqueConstraint(
            "project_id", "creation_request_id", name="uq_ie_recruitment_creation"
        ),
        ForeignKeyConstraint(
            ["public_revision_id", "id"],
            ["ie_recruitment_revisions.id", "ie_recruitment_revisions.recruitment_id"],
            name="fk_ie_recruitment_public",
            use_alter=True,
        ),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint(
            "status IN ('DRAFT','OPEN','CLOSED','EXPIRED')", name="status_valid"
        ),
        CheckConstraint(
            "moderation_state IN ('NORMAL','TAKEN_DOWN')", name="moderation_valid"
        ),
        CheckConstraint(
            "char_length(creation_fingerprint) = 64", name="fingerprint_length"
        ),
        CheckConstraint(
            "jsonb_typeof(fields) = 'object' "
            "AND jsonb_typeof(fields->'contacts') = 'array' "
            "AND jsonb_array_length(fields->'contacts') <= 4",
            name="fields_shape",
        ),
        CheckConstraint(
            "fields->>'headcount' IS NULL OR "
            "(jsonb_typeof(fields->'headcount') = 'number' "
            "AND (fields->>'headcount')::numeric BETWEEN 1 AND 999 "
            "AND (fields->>'headcount')::numeric "
            "= trunc((fields->>'headcount')::numeric))",
            name="headcount_valid",
        ),
        CheckConstraint(
            "status = 'DRAFT' OR "
            "(public_revision_id IS NOT NULL AND cycle_id IS NOT NULL)",
            name="published_consistent",
        ),
        Index("ix_ie_recruitment_project", "project_id", "updated_at", "id"),
        Index(
            "ix_ie_recruitment_due",
            "next_remind_at",
            "id",
            postgresql_where=text("status = 'OPEN' AND moderation_state = 'NORMAL'"),
        ),
        Index(
            "ix_ie_recruitment_deadline",
            "deadline_at",
            "id",
            postgresql_where=text("status = 'OPEN' AND moderation_state = 'NORMAL'"),
        ),
        Index("ix_ie_recruitment_public_revision", "public_revision_id", "id"),
    )
    id: Mapped[UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    project_id: Mapped[UUID] = mapped_column(ForeignKey("ie_project_drafts.id"))
    creation_request_id: Mapped[UUID] = mapped_column()
    creation_fingerprint: Mapped[str] = mapped_column(String(64))
    fields: Mapped[dict[str, Any]] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    moderation_state: Mapped[str] = mapped_column(String(16))
    public_revision_id: Mapped[UUID | None] = mapped_column()
    cycle_id: Mapped[UUID | None] = mapped_column()
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_remind_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RecruitmentRevision(Base):
    __tablename__ = "ie_recruitment_revisions"
    __table_args__ = (
        *_text_bounds("content"),
        UniqueConstraint(
            "id", "recruitment_id", name="uq_ie_recruitment_revision_identity"
        ),
        UniqueConstraint(
            "recruitment_id", "number", name="uq_ie_recruitment_revision_number"
        ),
        CheckConstraint("number > 0", name="number_positive"),
        CheckConstraint(
            "jsonb_typeof(content) = 'object' AND NOT content ? 'contacts'",
            name="contact_free",
        ),
        CheckConstraint(
            "(content->>'headcount')::integer BETWEEN 1 AND 999", name="headcount_valid"
        ),
    )
    id: Mapped[UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    recruitment_id: Mapped[UUID] = mapped_column(ForeignKey("ie_recruitment_drafts.id"))
    number: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RecruitmentContactAuthorization(Base):
    __tablename__ = "ie_recruitment_contacts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["revision_id", "recruitment_id"],
            ["ie_recruitment_revisions.id", "ie_recruitment_revisions.recruitment_id"],
            name="fk_ie_recruitment_contact_revision",
        ),
        UniqueConstraint(
            "revision_id", "channel", name="uq_ie_recruitment_contact_channel"
        ),
        CheckConstraint(
            "channel IN ('WECHAT','QQ','EMAIL','PHONE')", name="channel_valid"
        ),
        CheckConstraint("char_length(value) BETWEEN 1 AND 254", name="value_length"),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= authorized_at", name="revocation_time"
        ),
        Index("ix_ie_recruitment_contact_parent", "recruitment_id", "revision_id"),
    )
    id: Mapped[UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    recruitment_id: Mapped[UUID] = mapped_column()
    revision_id: Mapped[UUID] = mapped_column()
    channel: Mapped[str] = mapped_column(String(16))
    value: Mapped[str] = mapped_column(Text)
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RecruitmentActionReceipt(Base):
    __tablename__ = "ie_recruitment_receipts"
    __table_args__ = (
        CheckConstraint("applied_version > 0", name="version_positive"),
        CheckConstraint("char_length(fingerprint) = 64", name="fingerprint_length"),
        CheckConstraint(
            "action IN ('update','publish','close','reopen',"
            "'confirm_active','revoke_contact')",
            name="action_valid",
        ),
    )
    recruitment_id: Mapped[UUID] = mapped_column(
        ForeignKey("ie_recruitment_drafts.id"), primary_key=True
    )
    request_id: Mapped[UUID] = mapped_column(primary_key=True)
    action: Mapped[str] = mapped_column(String(24))
    fingerprint: Mapped[str] = mapped_column(String(64))
    applied_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
