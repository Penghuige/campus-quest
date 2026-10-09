"""Private evidence state; checked bytes and history references are immutable."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
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


class AchievementEvidence(Base):
    __tablename__ = "ie_achievement_evidence"
    __table_args__ = (
        UniqueConstraint("id", "achievement_id", name="uq_ie_evidence_identity"),
        UniqueConstraint(
            "achievement_id",
            "creation_request_id",
            name="uq_ie_evidence_creation_request",
        ),
        UniqueConstraint("object_key", name="uq_ie_evidence_object_key"),
        CheckConstraint("size BETWEEN 1 AND 10485760", name="size_bound"),
        CheckConstraint(
            "content_type IN ('application/pdf','image/png','image/jpeg')",
            name="content_type",
        ),
        CheckConstraint(
            "state IN ('PENDING','CHECKING','READY','REJECTED')", name="state"
        ),
        CheckConstraint("(state = 'READY') = (sha256 IS NOT NULL)", name="ready_hash"),
        CheckConstraint(
            "sha256 IS NULL OR char_length(sha256) = 64", name="hash_length"
        ),
        CheckConstraint(
            "(state = 'CHECKING') = (check_token IS NOT NULL) "
            "AND (state = 'CHECKING') = (checking_until IS NOT NULL)",
            name="check_lease",
        ),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint(
            "char_length(creation_payload_fingerprint) = 64", name="fingerprint_length"
        ),
        Index(
            "ix_ie_evidence_achievement_created", "achievement_id", "created_at", "id"
        ),
    )
    id: Mapped[UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    achievement_id: Mapped[UUID] = mapped_column(ForeignKey("ie_achievement_drafts.id"))
    creation_request_id: Mapped[UUID] = mapped_column()
    creation_payload_fingerprint: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(40))
    size: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), server_default=text("'PENDING'"))
    sha256: Mapped[str | None] = mapped_column(String(64))
    failure_code: Mapped[str | None] = mapped_column(String(40))
    check_token: Mapped[UUID | None] = mapped_column()
    checking_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    upload_url: Mapped[str] = mapped_column(Text)
    client_headers: Mapped[dict[str, str]] = mapped_column(JSONB)
    upload_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    referenced: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
