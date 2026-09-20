# backend/app/modules/submissions/models.py
"""Submission module persistence models (spec §10-13; invariant §31.11).

Design decisions:

- Enum-like columns are VARCHAR with explicitly named CHECK constraints,
  not PostgreSQL native enums — same rationale as the tasks module. The
  short CHECK names compose with the naming convention in `app.db.base`
  into e.g. `ck_submissions_validation_status` (the 0003 gotcha).
- `version` uniqueness is scoped per Claim (§31.11) through
  UNIQUE(claim_id, version); monotonic allocation of the next version is a
  service duty (upload finalize, plan 04 task 2) — the database only
  guarantees that a duplicated version cannot be committed.
- `object_key` is the server-generated object-storage key (spec §10: never
  derived from the client filename) and globally unique: one stored object
  backs at most one submission row. `original_filename` is display-only
  metadata, length-capped at 255 and sanitized by the service before
  storage; it must never feed paths or type decisions.
- `declared_type` is the client's claim, `detected_type` the sniffed
  content type written by the validation worker; both are confined to the
  CSV/XLSX/SQLITE universe the tasks module already guards.
- `submitted_at` has no server default on purpose: spec §11.2 fixes it as
  the reward-tier reference time, so the finalize service must pass the
  business clock value explicitly instead of letting the database default
  to worker/commit time.
- `validation_report` on the submission is the latest run's structured
  report (§12.4) for cheap review-page reads; the full per-run history —
  retries included — lives in `submission_validations` (one row per
  validation run). Re-runs are gated by terminal states at service level,
  not by the schema.
- Retention contract (spec §13): every row carries exactly one of a
  `retention_until` snapshot (computed from the Task's retention policy at
  submission time; later Task edits must not rewrite it) or the explicit
  `retention_permanent` flag — the coherence CHECK makes both set and
  neither set unrepresentable, so "permanent" can never be faked with a
  huge sentinel date. `legal_hold` blocks the cleanup worker regardless of
  the deadline; `deleted_at` marks the raw object's removal while business
  metadata, validation reports, and audit rows survive.
- `SubmissionReview` and `RewardLockHistory` are append-only audit history
  (spec §11.2: INVALIDATED lock transitions must never be overwritten).
  The database deliberately does not enforce append-only; immutability is
  a service convention — no UPDATE path will exist — and the tables
  declare no onupdate columns.
- `RewardLockHistory` records lock transitions with the tier fraction and
  locked points at transition time, mirroring the
  `AssignmentClaim.reward_tier_locked` / `locked_reward_points`
  projection column names for greppability.
- `AssignmentClaim.latest_submission_id` stays a plain UUID with no FK
  from the claims side: submissions already reference claims, and a
  reciprocal FK pair would be circular (a claim's latest pointer must be
  insertable before/at submission time without ordering deadlocks).
  Consistency is kept by the service writing both rows in one transaction;
  the UNIQUE(claim_id, version) constraint is the database-side anchor.
- No ORM relationships are declared yet; navigation joins arrive with the
  services that need them (backend-engineering §8).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Submission(Base):
    """One submitted file version under a Claim (spec §11)."""

    __tablename__ = "submissions"
    __table_args__ = (
        CheckConstraint(
            "validation_status IN "
            "('UPLOADED', 'VALIDATING', 'VALIDATED', 'VALIDATION_FAILED')",
            name="validation_status",
        ),
        CheckConstraint(
            "review_status IN "
            "('PENDING_REVIEW', 'UNDER_REVIEW', 'APPROVED', 'REVISION_REQUIRED')",
            name="review_status",
        ),
        CheckConstraint(
            "declared_type IN ('CSV', 'XLSX', 'SQLITE')",
            name="declared_type",
        ),
        CheckConstraint(
            "detected_type IS NULL OR detected_type IN ('CSV', 'XLSX', 'SQLITE')",
            name="detected_type",
        ),
        CheckConstraint("file_size >= 0", name="file_size"),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint(
            "(retention_permanent AND retention_until IS NULL) "
            "OR (NOT retention_permanent AND retention_until IS NOT NULL)",
            name="retention_exclusive",
        ),
        UniqueConstraint("claim_id", "version", name="uq_submissions_claim_id_version"),
        UniqueConstraint("object_key", name="uq_submissions_object_key"),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    claim_id: Mapped[UUID] = mapped_column(
        ForeignKey("assignment_claims.id"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    # Server-generated storage key (spec §10); never the client filename.
    object_key: Mapped[str] = mapped_column(Text)
    # Display-only metadata (spec §10): capped, sanitized by the service,
    # never used for paths or type decisions.
    original_filename: Mapped[str] = mapped_column(String(255))
    declared_type: Mapped[str] = mapped_column(String(16))
    detected_type: Mapped[str | None] = mapped_column(String(16))
    file_size: Mapped[int] = mapped_column(BigInteger)
    # Reward-tier reference time (spec §11.2): set explicitly by the
    # finalize service from the business clock — no server default, so a
    # forgotten value cannot silently become worker/commit time.
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    validation_status: Mapped[str] = mapped_column(
        String(24), server_default=text("'UPLOADED'")
    )
    review_status: Mapped[str] = mapped_column(
        String(24), server_default=text("'PENDING_REVIEW'")
    )
    # Latest validation run's structured report (spec §12.4); per-run
    # history lives in submission_validations.
    validation_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reviewer_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)
    retention_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        comment=(
            "Retention snapshot computed from the Task's retention policy at "
            "submission time (spec §13); later Task edits must not rewrite it. "
            "NULL exactly when retention_permanent is true."
        ),
    )
    retention_permanent: Mapped[bool] = mapped_column(
        server_default=text("false"),
        comment=(
            "Explicit permanent-retention flag (spec §13): permanence is "
            "always this flag, never a huge sentinel date in retention_until."
        ),
    )
    legal_hold: Mapped[bool] = mapped_column(
        server_default=text("false"),
        comment=(
            "Blocks the cleanup worker regardless of retention_until (spec §13/§27)."
        ),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        comment=(
            "When the raw upload was removed from object storage by the "
            "cleanup worker (spec §13/§27); business metadata, validation "
            "reports, and audit rows survive the file. NULL while the "
            "object exists."
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class SubmissionValidation(Base):
    """One machine validation run over a submission (spec §12.4).

    A row is written when a run starts (status VALIDATING, report NULL)
    and completed with the structured report; retries append further rows.
    Re-runs after a terminal run are gated at service level.
    """

    __tablename__ = "submission_validations"
    __table_args__ = (
        # Run status is the ValidationStatus member set minus the pre-run
        # UPLOADED stage, which belongs to the submission, not a run.
        CheckConstraint(
            "status IN ('VALIDATING', 'VALIDATED', 'VALIDATION_FAILED')",
            name="status",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    submission_id: Mapped[UUID] = mapped_column(
        ForeignKey("submissions.id"), index=True
    )
    # Structured report (spec §12.4): parser_version, file_type, row_count,
    # detected_columns, error/warning lists, ... — filled when the run
    # reaches a terminal state.
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    parser_version: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24))


class SubmissionReview(Base):
    """Append-only teacher review decision history (spec §11.3/§14).

    Immutability is a service convention: no UPDATE path exists and no
    column carries onupdate; the database deliberately does not enforce
    append-only. The current projection lives on Submission
    (review_status/reviewer_id/reviewed_at/review_note).
    """

    __tablename__ = "submission_reviews"
    __table_args__ = (
        CheckConstraint(
            "action IN ('APPROVE', 'REQUIRE_REVISION', 'INVALIDATE_LOCK')",
            name="action",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    submission_id: Mapped[UUID] = mapped_column(
        ForeignKey("submissions.id"), index=True
    )
    reviewer_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(24))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class RewardLockHistory(Base):
    """Append-only reward-lock transition audit (spec §11.2).

    Every lock change appends a row with the tier fraction and locked
    points at transition time; INVALIDATED history must never be
    overwritten, and a later valid submission may open a fresh PROVISIONAL
    row. The current state is projected onto AssignmentClaim
    (reward_lock_status/reward_tier_locked/reward_locked_at/
    locked_reward_points); immutability is a service convention — no
    UPDATE path exists and no column carries onupdate.
    """

    __tablename__ = "reward_lock_history"
    __table_args__ = (
        CheckConstraint(
            "lock_status_from IS NULL OR lock_status_from "
            "IN ('NONE', 'PROVISIONAL', 'CONFIRMED', 'INVALIDATED')",
            name="lock_status_from",
        ),
        CheckConstraint(
            "lock_status_to IN ('NONE', 'PROVISIONAL', 'CONFIRMED', 'INVALIDATED')",
            name="lock_status_to",
        ),
        CheckConstraint(
            "locked_reward_points IS NULL OR locked_reward_points >= 0",
            name="locked_reward_points",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    claim_id: Mapped[UUID] = mapped_column(
        ForeignKey("assignment_claims.id"), index=True
    )
    # Nullable: transitions not attributable to a specific submission
    # (e.g. system-side changes) are representable; submission-driven
    # transitions always record which submission caused them.
    submission_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("submissions.id"), index=True
    )
    lock_status_from: Mapped[str | None] = mapped_column(String(16))
    lock_status_to: Mapped[str] = mapped_column(String(16))
    # Tier fraction (spec §9.3: 100/80/50/20) and points at transition
    # time; names mirror the AssignmentClaim projection columns.
    reward_tier_locked: Mapped[int | None] = mapped_column(Integer)
    locked_reward_points: Mapped[int | None] = mapped_column(Integer)
    changed_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
