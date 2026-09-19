# backend/app/modules/tasks/schemas.py
"""Task module commands and DTOs (spec §6, §25.1; backend-engineering §9).

Three shapes, mirroring the identity module's split:

- ``CreateTask`` / ``UpdateTask`` are internal command dataclasses, not
  Pydantic request models. The untrusted HTTP surface arrives with the
  task router (plan 03 task 9) and constructs these commands after its own
  parsing; raw caller strings are validated and normalized by
  ``TaskService``, never trusted here.
- ``TaskPublic`` is the privacy-safe read DTO: it enumerates its fields
  and is built explicitly ``from_domain`` — never serialized from the ORM
  object — so ``owner_teacher_id``, the submission schema, and any future
  internal column are unrepresentable in a response (spec §40). It stays
  minimal for V1 (availability counts arrive with the read side, T9).

Notification defaults are explicit-with-default because of the plan-03
task-1 carry: the columns' ``server_default true`` is only a fail-safe
fallback; the command layer is where "on unless configured off" (spec
§25.1: SMS on, EMAIL on for verified email, IN_APP on) becomes visible.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.modules.tasks.enums import DeadlineMode, TaskRarity, TaskStatus, TaskType
from app.modules.tasks.models import Task

# spec §25.1: every channel on by default; a Teacher/Admin may turn each
# one off per Task.
DEFAULT_NOTIFICATION_CHANNELS: tuple[str, ...] = ("SMS", "EMAIL", "IN_APP")


@dataclass(frozen=True, slots=True)
class CreateTask:
    """Command for ``TaskService.create_task`` (spec §6).

    The task is created DRAFT and invisible; completeness is enforced at
    publish, so DRAFT-legal incompleteness (empty file policy, missing
    schema, no deadline) is accepted here and rejected there. Values that
    the database would refuse anyway (reward <= 0, unsupported file types,
    oversize cap) fail fast with a friendly error instead of an
    IntegrityError. Datetimes must be timezone-aware (spec §9.3).

    ``grace_period_minutes`` is deliberately absent: spec §6 fixes it at
    1440 for V1 with no product entry point, so the service pins it.
    """

    title: str
    description: str
    base_reward_points: int
    deadline_mode: DeadlineMode
    allowed_file_types: Sequence[str]
    max_file_size_bytes: int
    task_type: TaskType = TaskType.DATA_CRAWL
    rarity: TaskRarity = TaskRarity.NORMAL
    fixed_deadline_at: datetime | None = None
    duration_minutes: int | None = None
    claim_cutoff_minutes: int = 240
    submission_schema: Mapping[str, Any] | None = None
    submission_schema_version: int | None = None
    # spec §25.1 defaults, explicit at the command layer (task-1 carry).
    notify_24h: bool = True
    notify_4h: bool = True
    notification_channels: Sequence[str] = DEFAULT_NOTIFICATION_CHANNELS


@dataclass(frozen=True, slots=True)
class UpdateTask:
    """Command for ``TaskService.update_task``; ``None`` means "unchanged".

    The field split is the V1 edit rule (spec §6.2): presentation fields
    (title, description, notify flags, channels) stay editable while a
    task is PUBLISHED or PAUSED; contract fields — everything else — are
    frozen from first publish on, because claims snapshot the contract at
    claim time and post-publish changes would create ambiguity against
    those snapshots. Providing any contract field on a PUBLISHED/PAUSED
    task is rejected, even with an identical value: the router's edit form
    sends presentation fields only. DRAFT accepts every field; CLOSED and
    ARCHIVED accept none.
    """

    # Presentation fields (editable in every non-terminal status).
    title: str | None = None
    description: str | None = None
    notify_24h: bool | None = None
    notify_4h: bool | None = None
    notification_channels: Sequence[str] | None = None
    # Contract fields (frozen once published; claims snapshot these).
    base_reward_points: int | None = None
    deadline_mode: DeadlineMode | None = None
    fixed_deadline_at: datetime | None = None
    duration_minutes: int | None = None
    claim_cutoff_minutes: int | None = None
    submission_schema: Mapping[str, Any] | None = None
    submission_schema_version: int | None = None
    allowed_file_types: Sequence[str] | None = None
    max_file_size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class TaskPublic:
    """Privacy-safe Task read DTO (spec §6, §40).

    Deliberately minimal for V1: owner identity, submission schema, file
    policy details, and notification config are not part of the student-
    facing shape; availability counts join with the read side (T9).
    """

    id: UUID
    title: str
    description: str
    task_type: str
    rarity: str
    base_reward_points: int
    status: str
    deadline_mode: str
    fixed_deadline_at: datetime | None
    duration_minutes: int | None
    created_at: datetime

    @classmethod
    def from_domain(cls, task: Task) -> TaskPublic:
        """Build the DTO explicitly from a Task row; nothing else leaks."""
        return cls(
            id=task.id,
            title=task.title,
            description=task.description,
            task_type=task.task_type,
            rarity=task.rarity,
            base_reward_points=task.base_reward_points,
            status=task.status,
            deadline_mode=task.deadline_mode,
            fixed_deadline_at=task.fixed_deadline_at,
            duration_minutes=task.duration_minutes,
            created_at=task.created_at,
        )


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Outcome of a successful transition into PUBLISHED.

    ``claimable`` is the same ``TaskService.is_claimable`` verdict the
    claim side (T6) will enforce, computed at publish time — a FIXED task
    published exactly at its cutoff edge reports True; anything closer is
    rejected outright (spec §9.1), so a published task carrying False can
    only mean the deadline crossed the cutoff between publish and read.
    """

    task_id: UUID
    status: TaskStatus
    published_at: datetime
    claimable: bool
