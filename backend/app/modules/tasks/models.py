# backend/app/modules/tasks/models.py
"""Task module persistence models (spec §6-8; invariants §31.3-31.5).

Design decisions:

- Enum-like columns are VARCHAR with explicitly named CHECK constraints,
  not PostgreSQL native enums — same rationale as the identity module
  (adding a member is a constraint swap, not ALTER TYPE). The short CHECK
  names compose with the naming convention in `app.db.base` into e.g.
  `ck_tasks_status`; a full "ck_tasks_status" here would render doubled
  (the 0002 gotcha).
- Collaborator capabilities are a PostgreSQL ARRAY of VARCHAR guarded by a
  CHECK (`<@` against the closed capability set from plan 03 task 3:
  VIEW_TASK / MANAGE_ASSIGNMENTS / REVIEW_SUBMISSIONS / MODERATE_COMMUNITY),
  not a free-form JSONB blob: the database boundary itself rejects unknown
  or misspelled capabilities. Grant semantics (who may grant what) stay in
  the service layer.
- `allowed_file_types` and `notification_channels` follow the same
  ARRAY + `<@` CHECK pattern. `notification_channels` values are the frozen
  `NotificationChannel` members (SMS/EMAIL/IN_APP, interfaces.md); the enum
  class itself arrives with the notification module (plan 07). An empty
  `allowed_file_types` array is representable but useless — publish-time
  validation (plan 03 task 2) requires a non-empty file policy, a deadline
  policy, and a submission schema, so DRAFT rows may legitimately carry
  incomplete configuration and there is deliberately no table-level
  deadline-mode/fixed-deadline consistency CHECK either.
- `grace_period_minutes` is NOT NULL with server_default 1440 but is not
  CHECK-pinned to 1440: spec §6 fixes the value for V1 by not offering a
  product entry point, not as an eternal database invariant — pinning it
  would turn any future configurability into a constraint migration.
- `Assignment.keyword` is TEXT compared byte-exactly (spec §7: 中文场景按
  精确文本). "trim-stored" is a service-layer write-time normalization;
  the UNIQUE(task_id, platform, keyword) constraint then deduplicates the
  normalized values. Platform is a controlled lowercase enum-like set.
- Claim uniqueness uses two partial unique indexes over the same
  non-terminal status set (CLAIMED/VALIDATING/UNDER_REVIEW/
  REVISION_REQUIRED): one active Claim per Assignment (§31.4) and one
  non-terminal Claim per user per Task (§31.5). Terminal statuses
  (COMPLETED/ABANDONED/EXPIRED, interfaces.md) drop out of both indexes,
  which is exactly what re-claiming after ABANDONED/EXPIRED requires
  (§8.2). Both WHERE clauses are built from `ACTIVE_CLAIM_STATUSES` so the
  models, migration, and later claim service share one definition.
- Claims snapshot the task contract at claim time (§6.2): reward policy,
  base points, deadline, grace deadline; `task_id` is denormalized onto
  the claim so the (user_id, task_id) partial index and per-task history
  queries never need a join. Later Task edits must not touch these.
- `reward_tier_locked` is the locked percentage from the snapshotted
  ladder (spec §9.3: 100/80/50/20), not a string tier name — the ladder
  shape itself is versioned inside `reward_policy_snapshot`.
- `latest_submission_id` is a plain nullable UUID with no FK: the
  submissions table arrives in plan 04, which will add the FK then.
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
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.modules.tasks.enums import ClaimStatus

# Claim statuses that hold an Assignment/user slot (spec §8.2, §31.4-31.5).
# COMPLETED/ABANDONED/EXPIRED are terminal and excluded from both partial
# unique indexes below; the claim service (plan 03 task 6) reuses this
# tuple for its availability predicates.
ACTIVE_CLAIM_STATUSES: tuple[ClaimStatus, ...] = (
    ClaimStatus.CLAIMED,
    ClaimStatus.VALIDATING,
    ClaimStatus.UNDER_REVIEW,
    ClaimStatus.REVISION_REQUIRED,
)

_ACTIVE_CLAIM_STATUS_LIST = ", ".join(
    f"'{status.value}'" for status in ACTIVE_CLAIM_STATUSES
)
_WHERE_CLAIM_IS_ACTIVE = text(f"status IN ({_ACTIVE_CLAIM_STATUS_LIST})")

# Closed capability and value sets guarded at the database boundary.
_COLLABORATOR_CAPABILITIES = (
    "VIEW_TASK",
    "MANAGE_ASSIGNMENTS",
    "REVIEW_SUBMISSIONS",
    "MODERATE_COMMUNITY",
)
_ALLOWED_FILE_TYPES = ("CSV", "XLSX", "SQLITE")  # spec §10/§12
_NOTIFICATION_CHANNELS = ("SMS", "EMAIL", "IN_APP")  # interfaces.md §25


def _contained_by(values: tuple[str, ...]) -> str:
    """SQL array-containment literal, e.g. ``<@ ARRAY['CSV']::varchar[]``."""
    members = ", ".join(f"'{value}'" for value in values)
    return f"<@ ARRAY[{members}]::varchar[]"


class Task(Base):
    """A teacher-owned participatable task (spec §6)."""

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "task_type IN ('DATA_CRAWL')",
            name="task_type",
        ),
        CheckConstraint(
            "rarity IN ('NORMAL', 'RARE', 'EPIC', 'LEGENDARY')",
            name="rarity",
        ),
        CheckConstraint(
            "base_reward_points > 0",
            name="base_reward_points",
        ),
        CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED', 'PAUSED', 'CLOSED', 'ARCHIVED')",
            name="status",
        ),
        CheckConstraint(
            "deadline_mode IN ('FIXED', 'RELATIVE')",
            name="deadline_mode",
        ),
        CheckConstraint(
            "duration_minutes IS NULL OR duration_minutes > 0",
            name="duration_minutes",
        ),
        CheckConstraint(
            "grace_period_minutes > 0",
            name="grace_period_minutes",
        ),
        CheckConstraint(
            "claim_cutoff_minutes >= 0",
            name="claim_cutoff_minutes",
        ),
        CheckConstraint(
            f"allowed_file_types {_contained_by(_ALLOWED_FILE_TYPES)}",
            name="allowed_file_types",
        ),
        CheckConstraint(
            "max_file_size_bytes > 0",
            name="max_file_size_bytes",
        ),
        CheckConstraint(
            f"notification_channels {_contained_by(_NOTIFICATION_CHANNELS)}",
            name="notification_channels",
        ),
        CheckConstraint(
            "retention_policy IN ('DAYS_30', 'DAYS_90', 'DAYS_180', 'PERMANENT')",
            name="retention_policy",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    owner_teacher_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text)
    task_type: Mapped[str] = mapped_column(String(32))
    rarity: Mapped[str] = mapped_column(String(16))
    base_reward_points: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    deadline_mode: Mapped[str] = mapped_column(String(16))
    fixed_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_minutes: Mapped[int | None] = mapped_column(Integer)
    # V1 fixes grace at 1440 minutes (spec §6); see module docstring for why
    # this is a server default rather than a CHECK-pinned constant.
    grace_period_minutes: Mapped[int] = mapped_column(
        Integer, server_default=text("1440")
    )
    claim_cutoff_minutes: Mapped[int] = mapped_column(
        Integer, server_default=text("240")
    )
    submission_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    submission_schema_version: Mapped[int | None] = mapped_column(Integer)
    allowed_file_types: Mapped[list[str]] = mapped_column(ARRAY(String(16)))
    max_file_size_bytes: Mapped[int] = mapped_column(BigInteger)
    notify_24h: Mapped[bool] = mapped_column(server_default=text("true"))
    notify_4h: Mapped[bool] = mapped_column(server_default=text("true"))
    notification_channels: Mapped[list[str]] = mapped_column(ARRAY(String(16)))
    retention_policy: Mapped[str] = mapped_column(
        String(16), server_default=text("'DAYS_180'")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=func.now()
    )


class TaskCollaborator(Base):
    """Teacher granted an explicit capability set on someone else's Task
    (spec §4.2; grant rules live in the collaborator service)."""

    __tablename__ = "task_collaborators"
    __table_args__ = (
        CheckConstraint(
            f"permissions {_contained_by(_COLLABORATOR_CAPABILITIES)}",
            name="permissions",
        ),
        CheckConstraint(
            "array_length(permissions, 1) > 0",
            name="permissions_nonempty",
        ),
        UniqueConstraint(
            "task_id", "teacher_id", name="uq_task_collaborators_task_id_teacher_id"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id"))
    teacher_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    permissions: Mapped[list[str]] = mapped_column(ARRAY(String(32)))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class Assignment(Base):
    """Pre-seeded work unit under a Task (spec §7).

    Availability is derived state maintained by the claim service; the
    COMPLETED/RETIRED values are the sticky ones (a COMPLETED Assignment
    never returns to AVAILABLE, §8.2), while ABANDONED/EXPIRED claims flip
    the row back to AVAILABLE without touching it here.
    """

    __tablename__ = "assignments"
    __table_args__ = (
        CheckConstraint(
            "platform IN ('xiaohongshu', 'douyin', 'zhihu')",
            name="platform",
        ),
        CheckConstraint(
            "availability_status IN ('AVAILABLE', 'OCCUPIED', 'COMPLETED', 'RETIRED')",
            name="availability_status",
        ),
        UniqueConstraint(
            "task_id",
            "platform",
            "keyword",
            name="uq_assignments_task_id_platform_keyword",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id"), index=True)
    platform: Mapped[str] = mapped_column(String(32))
    keyword: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    availability_status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class AssignmentClaim(Base):
    """One claim (assignment-occupancy) history row (spec §8).

    Snapshots (§6.2): the reward policy, base points, deadline and grace
    deadline are copied from the Task at claim time and never follow later
    Task edits.
    """

    __tablename__ = "assignment_claims"
    __table_args__ = (
        CheckConstraint(
            "status IN ('CLAIMED', 'VALIDATING', 'UNDER_REVIEW', "
            "'REVISION_REQUIRED', 'COMPLETED', 'ABANDONED', 'EXPIRED')",
            name="status",
        ),
        CheckConstraint(
            "reward_lock_status IN ('NONE', 'PROVISIONAL', 'CONFIRMED', 'INVALIDATED')",
            name="reward_lock_status",
        ),
        CheckConstraint(
            "locked_reward_points IS NULL OR locked_reward_points >= 0",
            name="locked_reward_points",
        ),
        Index(
            "uq_assignment_claims_active_assignment",
            "assignment_id",
            unique=True,
            postgresql_where=_WHERE_CLAIM_IS_ACTIVE,
        ),
        Index(
            "uq_assignment_claims_active_user_task",
            "user_id",
            "task_id",
            unique=True,
            postgresql_where=_WHERE_CLAIM_IS_ACTIVE,
        ),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    assignment_id: Mapped[UUID] = mapped_column(
        ForeignKey("assignments.id"), index=True
    )
    # Denormalized from Assignment for the partial unique index and history
    # queries (see module docstring); kept honest by the claim service
    # writing both columns from the same Assignment row.
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(24))
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    grace_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reward_policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    base_reward_points_snapshot: Mapped[int] = mapped_column(Integer)
    reward_lock_status: Mapped[str] = mapped_column(String(16))
    # Locked percentage from the snapshotted ladder (spec §9.3).
    reward_tier_locked: Mapped[int | None] = mapped_column(Integer)
    reward_locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_reward_points: Mapped[int | None] = mapped_column(Integer)
    # Plain UUID on purpose: submissions arrive in plan 04, which adds the FK.
    latest_submission_id: Mapped[UUID | None] = mapped_column()
    revision_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
