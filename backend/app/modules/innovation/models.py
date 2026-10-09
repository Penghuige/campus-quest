"""Innovation preparation and explicit owner qualification; no publication."""

from datetime import datetime
from typing import Literal
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
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AchievementDraft(Base):
    """Private child content; no approval/publication state is implied."""

    __tablename__ = "ie_achievement_drafts"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "creation_request_id",
            name="uq_ie_achievement_drafts_project_request",
        ),
        CheckConstraint(
            "char_length(title) BETWEEN 1 AND 120 AND btrim(title) <> ''",
            name="title_length",
        ),
        CheckConstraint("char_length(description) <= 4000", name="description_length"),
        CheckConstraint(
            "char_length(work_url) <= 2000 AND (work_url = '' OR work_url ~* '^https?://[^/]+')",
            name="work_url_format",
        ),
        CheckConstraint("char_length(award_text) <= 1000", name="award_text_length"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint(
            "char_length(creation_payload_fingerprint) = 64", name="fingerprint_length"
        ),
        Index(
            "ix_ie_achievement_drafts_project_updated",
            "project_id",
            text("updated_at DESC"),
            text("id DESC"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    project_id: Mapped[UUID] = mapped_column(ForeignKey("ie_project_drafts.id"))
    creation_request_id: Mapped[UUID] = mapped_column()
    creation_payload_fingerprint: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    work_url: Mapped[str] = mapped_column(Text, server_default=text("''"))
    award_text: Mapped[str] = mapped_column(Text, server_default=text("''"))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class OperationsGrant(Base):
    """Retained after revocation so stale versions cannot revoke a regrant."""

    __tablename__ = "ie_operations_grants"
    __table_args__ = (CheckConstraint("version > 0", name="version_positive"),)

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean)
    version: Mapped[int] = mapped_column(Integer)
    changed_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OwnerProfile(Base):
    __tablename__ = "ie_owner_profiles"
    __table_args__ = (
        CheckConstraint(
            "char_length(name) BETWEEN 1 AND 80 AND btrim(name) <> ''",
            name="name_length",
        ),
        CheckConstraint(
            "char_length(student_no) BETWEEN 1 AND 40 AND btrim(student_no) <> ''",
            name="student_no_length",
        ),
        CheckConstraint(
            "char_length(major) BETWEEN 1 AND 120 AND btrim(major) <> ''",
            name="major_length",
        ),
        CheckConstraint(
            "char_length(grade) BETWEEN 1 AND 40 AND btrim(grade) <> ''",
            name="grade_length",
        ),
        CheckConstraint("version > 0", name="version_positive"),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    student_no: Mapped[str] = mapped_column(String(40))
    major: Mapped[str] = mapped_column(String(120))
    grade: Mapped[str] = mapped_column(String(40))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class OwnerQualification(Base):
    """Submitted four-field snapshot; approval never means achievement review."""

    __tablename__ = "ie_owner_qualifications"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING', 'APPROVED')", name="status_valid"),
        CheckConstraint(
            "version > 0 AND profile_version > 0", name="versions_positive"
        ),
        CheckConstraint(
            "(status = 'PENDING' AND approved_at IS NULL AND approved_by IS NULL) OR "
            "(status = 'APPROVED' AND approved_at IS NOT NULL "
            "AND approved_by IS NOT NULL)",
            name="approval_consistent",
        ),
        CheckConstraint(
            "char_length(name) BETWEEN 1 AND 80 AND btrim(name) <> ''",
            name="name_length",
        ),
        CheckConstraint(
            "char_length(student_no) BETWEEN 1 AND 40 AND btrim(student_no) <> ''",
            name="student_no_length",
        ),
        CheckConstraint(
            "char_length(major) BETWEEN 1 AND 120 AND btrim(major) <> ''",
            name="major_length",
        ),
        CheckConstraint(
            "char_length(grade) BETWEEN 1 AND 40 AND btrim(grade) <> ''",
            name="grade_length",
        ),
        Index(
            "ix_ie_owner_qualifications_pending",
            "requested_at",
            "user_id",
            postgresql_where=text("status = 'PENDING'"),
        ),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    status: Mapped[Literal["PENDING", "APPROVED"]] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer)
    profile_version: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(80))
    student_no: Mapped[str] = mapped_column(String(40))
    major: Mapped[str] = mapped_column(String(120))
    grade: Mapped[str] = mapped_column(String(40))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))


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
