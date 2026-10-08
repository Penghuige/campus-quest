"""Private project drafts; no publication, achievement or qualification state."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ProjectDraft(Base):
    __tablename__ = "ie_project_drafts"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "creation_request_id",
            name="uq_ie_project_drafts_owner_request",
        ),
        CheckConstraint(
            "char_length(title) BETWEEN 1 AND 120 AND btrim(title) <> ''",
            name="title_length",
        ),
        CheckConstraint("char_length(summary) <= 2000", name="summary_length"),
        CheckConstraint("char_length(direction) <= 120", name="direction_length"),
        CheckConstraint("char_length(stage) <= 80", name="stage_length"),
        CheckConstraint("char_length(team_status) <= 1000", name="team_status_length"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint(
            "char_length(creation_payload_fingerprint) = 64", name="fingerprint_length"
        ),
        Index(
            "ix_ie_project_drafts_owner_updated_at",
            "owner_user_id",
            text("updated_at DESC"),
            text("id DESC"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    owner_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    creation_request_id: Mapped[UUID] = mapped_column()
    # Immutable first-create fingerprint, never recalculated from the edited draft.
    creation_payload_fingerprint: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(120))
    summary: Mapped[str] = mapped_column(Text, server_default=text("''"))
    direction: Mapped[str] = mapped_column(String(120), server_default=text("''"))
    stage: Mapped[str] = mapped_column(String(80), server_default=text("''"))
    team_status: Mapped[str] = mapped_column(Text, server_default=text("''"))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
