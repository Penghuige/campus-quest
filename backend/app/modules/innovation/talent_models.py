"""Talent aggregate, immutable public history, receipts and checked files."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
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


class TalentProfile(Base):
    __tablename__ = "ie_talent_profiles"
    __table_args__ = (
        UniqueConstraint("owner_user_id", name="uq_ie_talent_owner"),
        ForeignKeyConstraint(
            ["public_revision_id", "id"],
            ["ie_talent_revisions.id", "ie_talent_revisions.profile_id"],
            name="fk_ie_talent_public",
            use_alter=True,
        ),
        CheckConstraint("version > 0 AND public_version >= 0", name="versions_valid"),
        CheckConstraint("visibility IN ('HIDDEN','VISIBLE')", name="visibility_valid"),
        CheckConstraint(
            "moderation_state IN ('NORMAL','TAKEN_DOWN')", name="moderation_valid"
        ),
        CheckConstraint(
            "visibility <> 'VISIBLE' OR "
            "(public_revision_id IS NOT NULL AND public_version > 0)",
            name="visible_consistent",
        ),
        CheckConstraint("jsonb_typeof(fields) = 'object'", name="fields_object"),
        Index("ix_ie_talent_public_revision", "public_revision_id", "id"),
        Index(
            "ix_ie_talent_visible_updated",
            "public_updated_at",
            "id",
            postgresql_where=text(
                "visibility = 'VISIBLE' AND moderation_state = 'NORMAL'"
            ),
        ),
    )
    id: Mapped[UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    owner_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    fields: Mapped[dict[str, Any]] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer)
    public_version: Mapped[int] = mapped_column(Integer)
    visibility: Mapped[str] = mapped_column(String(16))
    moderation_state: Mapped[str] = mapped_column(String(16))
    public_revision_id: Mapped[UUID | None] = mapped_column()
    public_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TalentRevision(Base):
    __tablename__ = "ie_talent_revisions"
    __table_args__ = (
        UniqueConstraint("id", "profile_id", name="uq_ie_talent_revision_identity"),
        UniqueConstraint("profile_id", "number", name="uq_ie_talent_revision_number"),
        CheckConstraint("number > 0", name="number_positive"),
        CheckConstraint(
            "jsonb_typeof(content) = 'object' AND NOT content ? 'contacts'",
            name="content_private_free",
        ),
        CheckConstraint(
            "jsonb_typeof(contacts) = 'array' AND jsonb_array_length(contacts) <= 4",
            name="contact_shape",
        ),
    )
    id: Mapped[UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    profile_id: Mapped[UUID] = mapped_column(ForeignKey("ie_talent_profiles.id"))
    number: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    contacts: Mapped[list[dict[str, str]]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TalentActionReceipt(Base):
    __tablename__ = "ie_talent_receipts"
    __table_args__ = (
        UniqueConstraint("actor_user_id", "request_id", name="uq_ie_talent_request"),
        CheckConstraint("char_length(fingerprint) = 64", name="fingerprint_valid"),
        CheckConstraint("applied_version >= 0", name="version_nonnegative"),
        Index("ix_ie_talent_receipt_profile", "profile_id"),
    )
    id: Mapped[UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    profile_id: Mapped[UUID | None] = mapped_column(ForeignKey("ie_talent_profiles.id"))
    request_id: Mapped[UUID] = mapped_column()
    action: Mapped[str] = mapped_column(String(40))
    fingerprint: Mapped[str] = mapped_column(String(64))
    applied_version: Mapped[int] = mapped_column(Integer)
    resource_id: Mapped[UUID]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TalentFile(Base):
    __tablename__ = "ie_talent_files"
    __table_args__ = (
        UniqueConstraint(
            "profile_id", "creation_request_id", name="uq_ie_talent_file_creation"
        ),
        UniqueConstraint("object_key", name="uq_ie_talent_file_object"),
        UniqueConstraint("id", "profile_id", name="uq_ie_talent_file_identity"),
        CheckConstraint("size BETWEEN 1 AND 10485760", name="size_valid"),
        CheckConstraint(
            "content_type IN ('application/pdf','image/png','image/jpeg')",
            name="type_valid",
        ),
        CheckConstraint(
            "state IN ('PENDING','CHECKING','READY','REJECTED')", name="state_valid"
        ),
        CheckConstraint("(state = 'READY') = (sha256 IS NOT NULL)", name="ready_hash"),
        CheckConstraint(
            "state <> 'CHECKING' OR "
            "(attempt_token IS NOT NULL AND attempt_started_at IS NOT NULL)",
            name="checking_token",
        ),
        CheckConstraint(
            "version > 0 AND char_length(creation_fingerprint) = 64",
            name="versions_valid",
        ),
        CheckConstraint("char_length(name) BETWEEN 1 AND 200", name="name_valid"),
    )
    id: Mapped[UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    profile_id: Mapped[UUID] = mapped_column(ForeignKey("ie_talent_profiles.id"))
    creation_request_id: Mapped[UUID]
    creation_fingerprint: Mapped[str] = mapped_column(String(64))
    upload_url: Mapped[str] = mapped_column(Text)
    upload_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    client_headers: Mapped[dict[str, str]] = mapped_column(JSONB)
    name: Mapped[str] = mapped_column(String(200))
    object_key: Mapped[str] = mapped_column(String(1024))
    content_type: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16))
    sha256: Mapped[str | None] = mapped_column(String(64))
    failure_code: Mapped[str | None] = mapped_column(String(50))
    attempt_token: Mapped[UUID | None]
    attempt_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TalentRevisionFile(Base):
    __tablename__ = "ie_talent_revision_files"
    __table_args__ = (
        ForeignKeyConstraint(
            ["revision_id", "profile_id"],
            ["ie_talent_revisions.id", "ie_talent_revisions.profile_id"],
            name="fk_ie_talent_file_revision",
        ),
        ForeignKeyConstraint(
            ["file_id", "profile_id"],
            ["ie_talent_files.id", "ie_talent_files.profile_id"],
            name="fk_ie_talent_revision_file",
        ),
        Index("ix_ie_talent_revision_file_identity", "file_id", "profile_id"),
    )
    revision_id: Mapped[UUID] = mapped_column(primary_key=True)
    file_id: Mapped[UUID] = mapped_column(primary_key=True)
    profile_id: Mapped[UUID]


class TalentTag(Base):
    __tablename__ = "ie_talent_tags"
    __table_args__ = (
        UniqueConstraint("name", name="uq_ie_talent_tag_name"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 50", name="name_valid"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    enabled: Mapped[bool] = mapped_column(Boolean)
    position: Mapped[int] = mapped_column(Integer)


class TalentTagSuggestion(Base):
    __tablename__ = "ie_talent_tag_suggestions"
    __table_args__ = (
        Index(
            "uq_ie_talent_pending_suggestion",
            "owner_user_id",
            "normalized_name",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
        CheckConstraint(
            "status IN ('PENDING','ACCEPTED','MERGED','REJECTED')", name="status_valid"
        ),
        CheckConstraint(
            "char_length(name) BETWEEN 1 AND 50 AND char_length(reason) <= 500",
            name="text_valid",
        ),
    )
    id: Mapped[UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    owner_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(50))
    normalized_name: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
