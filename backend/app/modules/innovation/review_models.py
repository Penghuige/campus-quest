"""Immutable saved revisions and independent first-review/moderation state."""

from datetime import datetime
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


class AchievementRevision(Base):
    __tablename__ = "ie_achievement_revisions"
    __table_args__ = (
        UniqueConstraint("id", "achievement_id", name="uq_ie_revision_identity"),
        UniqueConstraint(
            "achievement_id", "creation_request_id", name="uq_ie_revision_request"
        ),
        UniqueConstraint("achievement_id", "number", name="uq_ie_revision_number"),
        CheckConstraint(
            "number > 0 AND project_version > 0 AND achievement_version > 0",
            name="versions_positive",
        ),
        CheckConstraint("operation IN ('SUBMIT','UPDATE')", name="operation"),
        CheckConstraint(
            "char_length(creation_payload_fingerprint) = 64", name="fingerprint_length"
        ),
    )
    id: Mapped[UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    achievement_id: Mapped[UUID] = mapped_column(ForeignKey("ie_achievement_drafts.id"))
    number: Mapped[int] = mapped_column(Integer)
    creation_request_id: Mapped[UUID] = mapped_column()
    creation_payload_fingerprint: Mapped[str] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(10))
    project_version: Mapped[int] = mapped_column(Integer)
    achievement_version: Mapped[int] = mapped_column(Integer)
    project_content: Mapped[dict[str, str]] = mapped_column(JSONB)
    achievement_content: Mapped[dict[str, str]] = mapped_column(JSONB)
    owner_profile: Mapped[dict[str, str]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RevisionEvidence(Base):
    __tablename__ = "ie_revision_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["revision_id", "achievement_id"],
            ["ie_achievement_revisions.id", "ie_achievement_revisions.achievement_id"],
            name="fk_ie_revision_evidence_revision",
        ),
        ForeignKeyConstraint(
            ["evidence_id", "achievement_id"],
            ["ie_achievement_evidence.id", "ie_achievement_evidence.achievement_id"],
            name="fk_ie_revision_evidence_material",
        ),
        CheckConstraint("char_length(sha256) = 64", name="hash_length"),
    )
    revision_id: Mapped[UUID] = mapped_column(primary_key=True)
    evidence_id: Mapped[UUID] = mapped_column(primary_key=True)
    achievement_id: Mapped[UUID] = mapped_column()
    sha256: Mapped[str] = mapped_column(String(64))


class AchievementWorkflow(Base):
    __tablename__ = "ie_achievement_workflows"
    __table_args__ = (
        ForeignKeyConstraint(
            ["public_revision_id", "achievement_id"],
            ["ie_achievement_revisions.id", "ie_achievement_revisions.achievement_id"],
            name="fk_ie_workflow_public_revision",
        ),
        CheckConstraint(
            "first_review_state IN ('DRAFT','SUBMITTED','RETURNED','APPROVED')",
            name="review_state",
        ),
        CheckConstraint(
            "moderation_state IN ('NORMAL','TAKEN_DOWN')", name="moderation_state"
        ),
        CheckConstraint(
            "(first_review_state = 'APPROVED') = (public_revision_id IS NOT NULL) "
            "AND (first_review_state = 'APPROVED') = (first_approved_at IS NOT NULL) "
            "AND (first_review_state = 'APPROVED') = (latest_update_at IS NOT NULL)",
            name="public_state",
        ),
        CheckConstraint("version > 0", name="version_positive"),
    )
    achievement_id: Mapped[UUID] = mapped_column(
        ForeignKey("ie_achievement_drafts.id"), primary_key=True
    )
    first_review_state: Mapped[str] = mapped_column(String(16), server_default="DRAFT")
    moderation_state: Mapped[str] = mapped_column(String(16), server_default="NORMAL")
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    public_revision_id: Mapped[UUID | None] = mapped_column()
    first_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latest_update_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AchievementReviewCase(Base):
    __tablename__ = "ie_achievement_review_cases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["revision_id", "achievement_id"],
            ["ie_achievement_revisions.id", "ie_achievement_revisions.achievement_id"],
            name="fk_ie_case_revision",
        ),
        UniqueConstraint("revision_id", name="uq_ie_case_revision"),
        CheckConstraint(
            "status IN ('SUBMITTED','WITHDRAWN','APPROVED','RETURNED')", name="status"
        ),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint(
            "status <> 'RETURNED' OR (reason IS NOT NULL AND btrim(reason) <> '')",
            name="return_reason",
        ),
        CheckConstraint(
            "(status IN ('APPROVED','RETURNED')) = (decided_at IS NOT NULL) "
            "AND (status IN ('APPROVED','RETURNED')) = "
            "(decision_request_id IS NOT NULL)",
            name="decision_state",
        ),
        Index("ix_ie_review_queue", "status", "submitted_at", "id"),
        Index(
            "uq_ie_case_active",
            "achievement_id",
            unique=True,
            postgresql_where=text("status = 'SUBMITTED'"),
        ),
    )
    id: Mapped[UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    achievement_id: Mapped[UUID] = mapped_column(ForeignKey("ie_achievement_drafts.id"))
    revision_id: Mapped[UUID] = mapped_column()
    status: Mapped[str] = mapped_column(String(16), server_default="SUBMITTED")
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    assigned_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_request_id: Mapped[UUID | None] = mapped_column()
    decision_payload_fingerprint: Mapped[str | None] = mapped_column(String(64))
