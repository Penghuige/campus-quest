# backend/app/modules/tasks/commands.py
"""Task module internal commands (spec §6, §6.2, §25.1; backend-engineering
§9).

Single responsibility: the dataclass commands the task services consume.
They are NOT Pydantic request models — the untrusted HTTP surface carries
its own ``*Request`` models (``transport_schemas``) and constructs these
commands after its own parsing; raw caller strings are validated and
normalized by ``TaskService``, never trusted here.

Notification defaults are explicit-with-default by design: the columns'
``server_default true`` is only a fail-safe fallback; the command layer
is where "on unless configured off" (spec §25.1: SMS on, EMAIL on for
verified email, IN_APP on) becomes visible.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.modules.tasks.enums import DeadlineMode, TaskRarity, TaskType

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
    # Enum-typed fields accept the raw caller string too: the transport
    # layer passes request input through unvalidated and TaskService is
    # the single normalization authority (``_member_or``).
    deadline_mode: DeadlineMode | str
    allowed_file_types: Sequence[str]
    max_file_size_bytes: int
    task_type: TaskType | str = TaskType.DATA_CRAWL
    rarity: TaskRarity | str = TaskRarity.NORMAL
    fixed_deadline_at: datetime | None = None
    duration_minutes: int | None = None
    claim_cutoff_minutes: int = 240
    submission_schema: Mapping[str, Any] | None = None
    submission_schema_version: int | None = None
    # spec §25.1 defaults, explicit at the command layer.
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
    deadline_mode: DeadlineMode | str | None = None
    fixed_deadline_at: datetime | None = None
    duration_minutes: int | None = None
    claim_cutoff_minutes: int | None = None
    submission_schema: Mapping[str, Any] | None = None
    submission_schema_version: int | None = None
    allowed_file_types: Sequence[str] | None = None
    max_file_size_bytes: int | None = None
