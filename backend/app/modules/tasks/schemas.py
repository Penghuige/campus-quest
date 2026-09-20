# backend/app/modules/tasks/schemas.py
"""Task module internal read-side DTOs (spec §6, §8, §20, §42;
backend-engineering §9).

Single responsibility: the dataclass DTOs the task services and query
service produce, and the transport layer serializes. These are NOT wire
shapes — the request/response contract lives in ``transport_schemas``,
the write-side commands in ``commands``.

The read DTOs live in this module — not query_service.py — because the
Pydantic response models (``transport_schemas``) build from them and
query_service must not import the transport layer. ``ClaimView`` is the
ONLY student-facing shape allowed to carry an Assignment's
platform/keyword (spec §42: 领取后只展示分配给本人的单元), and only for
the claim's owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.modules.tasks.enums import TaskStatus
from app.modules.tasks.models import Task


@dataclass(frozen=True, slots=True)
class TaskPublic:
    """Privacy-safe Task read DTO (spec §6, §40).

    Deliberately minimal: owner identity, submission schema, file policy
    details, and notification config are not part of this shape. The
    read side builds the richer card/detail views (``TaskCard``,
    ``PublishedTaskDetail``) with availability counts; this stays the
    minimal shared shape services may return directly.
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
    claim side enforces, computed at publish time — a FIXED task
    published exactly at its cutoff edge reports True; anything closer is
    rejected outright (spec §9.1), so a published task carrying False can
    only mean the deadline crossed the cutoff between publish and read.
    """

    task_id: UUID
    status: TaskStatus
    published_at: datetime
    claimable: bool


# --- read-side DTOs (consumed by TaskQueryService, serialized by transport) --------


@dataclass(frozen=True, slots=True)
class RatingSummary:
    """The public rating aggregate of one Task (spec §20: 前台只展示聚合分
    和数量 — average and count, never who gave what).

    Defined here (not query_service.py) so the transport models can build
    from it without a runtime cycle; query_service re-exports it, which is
    where existing imports point.
    """

    average: float
    count: int


@dataclass(frozen=True, slots=True)
class ClaimView:
    """One claim exactly as its OWNER sees it (spec §8, §42).

    This is the ONLY student-facing DTO allowed to carry an Assignment's
    platform/keyword: 领取后不暴露其他 Assignment 列表，只展示分配给本人
    的具体 platform/keyword. It never leaves an owner-scoped response.
    """

    claim_id: UUID
    task_id: UUID
    task_title: str
    status: str
    platform: str
    keyword: str
    claimed_at: datetime
    deadline_at: datetime
    grace_deadline_at: datetime
    base_reward_points_snapshot: int


@dataclass(frozen=True, slots=True)
class TaskCard:
    """The spec §42 Task Card as a list item: title, rarity, base reward,
    deadline mode + remaining-time source (``fixed_deadline_at`` for FIXED;
    ``duration_minutes`` for RELATIVE — the countdown is UX, the server
    instant rules, spec §42), the AVAILABLE count — never the assignment
    list — and the rating slot (None until the community module wires
    the adapter)."""

    id: UUID
    title: str
    rarity: str
    base_reward_points: int
    deadline_mode: str
    fixed_deadline_at: datetime | None
    duration_minutes: int | None
    assignments_available: int
    rating: RatingSummary | None


@dataclass(frozen=True, slots=True)
class PublishedTaskDetail:
    """The public detail of one PUBLISHED task for an authenticated viewer:
    card facts plus description/type/published_at and the viewer's own
    non-terminal claim on it, if any (terminal history lives in
    ``GET /me/claims``)."""

    id: UUID
    title: str
    description: str
    task_type: str
    rarity: str
    base_reward_points: int
    deadline_mode: str
    fixed_deadline_at: datetime | None
    duration_minutes: int | None
    published_at: datetime | None
    assignments_available: int
    rating: RatingSummary | None
    my_claim: ClaimView | None
