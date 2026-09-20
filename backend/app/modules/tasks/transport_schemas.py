# backend/app/modules/tasks/transport_schemas.py
"""Task module transport contract: request and response models (spec §6,
§7.1, §25.1, §28, §42; backend-engineering §9).

Single responsibility: the task API's wire shapes, and nothing else.

- The ``*Request`` models are the untrusted transport input: plain
  Pydantic models carrying raw caller strings only — normalization and
  business validation belong to the services, which is why enum-ish
  fields stay ``str`` here and become typed fields on the internal
  commands (``commands``).
- The ``*Response`` models are the response contract. Each enumerates its
  fields and is built explicitly from a DTO or ORM object — never
  serialized from one — so ``owner_teacher_id`` and any future internal
  column are unrepresentable in a student response, privacy by
  construction (spec §40).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.tasks.commands import DEFAULT_NOTIFICATION_CHANNELS
from app.modules.tasks.enums import TaskRarity, TaskStatus, TaskType
from app.modules.tasks.models import Task
from app.modules.tasks.schemas import (
    ClaimView,
    PublishedTaskDetail,
    RatingSummary,
    TaskCard,
)

if TYPE_CHECKING:
    # Import-only (runtime would cycle: importer/query_service import the
    # internal schemas module, which this module builds on). The response
    # builders below are the only consumers, and they run against
    # instances, never the class.
    from app.modules.tasks.importer import (
        AssignmentImportError,
        AssignmentImportPreview,
    )
    from app.modules.tasks.query_service import TaskStatistics, TeacherTaskListItem


# --- transport request models (raw caller input, validated by services) ------------


class TaskCreateRequest(BaseModel):
    """Create one DRAFT task (spec §6). Enum-ish fields stay raw strings:
    the service is the single validation/normalization authority."""

    title: str
    description: str
    base_reward_points: int
    deadline_mode: str
    allowed_file_types: list[str]
    max_file_size_bytes: int
    task_type: str = TaskType.DATA_CRAWL.value
    rarity: str = TaskRarity.NORMAL.value
    fixed_deadline_at: datetime | None = None
    duration_minutes: int | None = None
    claim_cutoff_minutes: int = 240
    submission_schema: dict[str, Any] | None = None
    submission_schema_version: int | None = None
    notify_24h: bool = True
    notify_4h: bool = True
    notification_channels: list[str] = list(DEFAULT_NOTIFICATION_CHANNELS)


class TaskUpdateRequest(BaseModel):
    """Partial edit under the V1 edit rule; absent/None means unchanged.

    The field split is ``UpdateTask``'s (presentation vs contract); the
    service decides what the task's current status permits.
    """

    title: str | None = None
    description: str | None = None
    notify_24h: bool | None = None
    notify_4h: bool | None = None
    notification_channels: list[str] | None = None
    base_reward_points: int | None = None
    deadline_mode: str | None = None
    fixed_deadline_at: datetime | None = None
    duration_minutes: int | None = None
    claim_cutoff_minutes: int | None = None
    submission_schema: dict[str, Any] | None = None
    submission_schema_version: int | None = None
    allowed_file_types: list[str] | None = None
    max_file_size_bytes: int | None = None


class ClaimRequest(BaseModel):
    """Claim one random assignment of a task (spec §8.3).

    Deliberately fieldless AND ``extra="forbid"``: assignment choice is
    the server's (random, under lock) — a request that smuggles an
    ``assignment_id`` is a 422 before any service call, proven by test.
    """

    model_config = ConfigDict(extra="forbid")


class CollaboratorAddRequest(BaseModel):
    """Grant a capability set to a Teacher on a task (spec §4.2)."""

    permissions: list[str]


class ImportConfirmRequest(BaseModel):
    """Confirm one preview by its single-use server-side token (spec §7.1)."""

    preview_token: str = Field(min_length=1)


# --- transport response models (explicit builders, never ORM dumps) ----------------


class RatingSummaryResponse(BaseModel):
    """Average + count only (spec §20/§40: no rater identity)."""

    average: float
    count: int

    @classmethod
    def from_domain(cls, summary: RatingSummary) -> RatingSummaryResponse:
        return cls(average=summary.average, count=summary.count)


class TaskCardResponse(BaseModel):
    """The §42 Task Card wire shape."""

    id: UUID
    title: str
    rarity: str
    base_reward_points: int
    deadline_mode: str
    fixed_deadline_at: datetime | None
    duration_minutes: int | None
    assignments_available: int
    rating: RatingSummaryResponse | None

    @classmethod
    def from_view(cls, card: TaskCard) -> TaskCardResponse:
        return cls(
            id=card.id,
            title=card.title,
            rarity=card.rarity,
            base_reward_points=card.base_reward_points,
            deadline_mode=card.deadline_mode,
            fixed_deadline_at=card.fixed_deadline_at,
            duration_minutes=card.duration_minutes,
            assignments_available=card.assignments_available,
            rating=(
                RatingSummaryResponse.from_domain(card.rating)
                if card.rating is not None
                else None
            ),
        )


class TaskListResponse(BaseModel):
    """Offset-paginated card page (the documented V1 choice)."""

    items: list[TaskCardResponse]
    total: int
    limit: int
    offset: int


class ClaimResponse(BaseModel):
    """A claim on the owner's own surface (spec §8, §42): the assigned
    unit's platform/keyword appear here and nowhere else student-facing."""

    claim_id: UUID
    task_id: UUID
    status: str
    platform: str
    keyword: str
    claimed_at: datetime
    deadline_at: datetime
    grace_deadline_at: datetime
    base_reward_points_snapshot: int

    @classmethod
    def from_view(cls, view: ClaimView) -> ClaimResponse:
        return cls(
            claim_id=view.claim_id,
            task_id=view.task_id,
            status=view.status,
            platform=view.platform,
            keyword=view.keyword,
            claimed_at=view.claimed_at,
            deadline_at=view.deadline_at,
            grace_deadline_at=view.grace_deadline_at,
            base_reward_points_snapshot=view.base_reward_points_snapshot,
        )


class MyClaimResponse(ClaimResponse):
    """A /me/claims item: the claim view plus the task's title (the list is
    cross-task, so the card context travels with the claim)."""

    task_title: str

    @classmethod
    def from_view(cls, view: ClaimView) -> MyClaimResponse:
        return cls(
            claim_id=view.claim_id,
            task_id=view.task_id,
            task_title=view.task_title,
            status=view.status,
            platform=view.platform,
            keyword=view.keyword,
            claimed_at=view.claimed_at,
            deadline_at=view.deadline_at,
            grace_deadline_at=view.grace_deadline_at,
            base_reward_points_snapshot=view.base_reward_points_snapshot,
        )


class MyClaimsResponse(BaseModel):
    """Offset-paginated own-claim history (current first)."""

    items: list[MyClaimResponse]
    total: int
    limit: int
    offset: int


class TaskDetailResponse(BaseModel):
    """Public detail of one PUBLISHED task plus the viewer's own claim."""

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
    published_at: datetime | None
    assignments_available: int
    rating: RatingSummaryResponse | None
    my_claim: ClaimResponse | None

    @classmethod
    def from_view(cls, detail: PublishedTaskDetail) -> TaskDetailResponse:
        return cls(
            id=detail.id,
            title=detail.title,
            description=detail.description,
            task_type=detail.task_type,
            rarity=detail.rarity,
            base_reward_points=detail.base_reward_points,
            status=TaskStatus.PUBLISHED.value,
            deadline_mode=detail.deadline_mode,
            fixed_deadline_at=detail.fixed_deadline_at,
            duration_minutes=detail.duration_minutes,
            published_at=detail.published_at,
            assignments_available=detail.assignments_available,
            rating=(
                RatingSummaryResponse.from_domain(detail.rating)
                if detail.rating is not None
                else None
            ),
            my_claim=(
                ClaimResponse.from_view(detail.my_claim)
                if detail.my_claim is not None
                else None
            ),
        )


class TeacherTaskResponse(BaseModel):
    """The staff workbench's full view of a task (spec §41): everything the
    owner configured, including contract fields frozen at first publish.
    ``owner_teacher_id`` stays out — staff know whose surface they called,
    and the id is not needed to operate the workbench (spec §40)."""

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
    claim_cutoff_minutes: int
    grace_period_minutes: int
    submission_schema: dict[str, Any] | None
    submission_schema_version: int | None
    allowed_file_types: list[str]
    max_file_size_bytes: int
    notify_24h: bool
    notify_4h: bool
    notification_channels: list[str]
    published_at: datetime | None
    closed_at: datetime | None
    created_at: datetime

    @classmethod
    def from_domain(cls, task: Task) -> TeacherTaskResponse:
        """Build explicitly from a Task row; future columns must opt in."""
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
            claim_cutoff_minutes=task.claim_cutoff_minutes,
            grace_period_minutes=task.grace_period_minutes,
            submission_schema=task.submission_schema,
            submission_schema_version=task.submission_schema_version,
            allowed_file_types=task.allowed_file_types,
            max_file_size_bytes=task.max_file_size_bytes,
            notify_24h=task.notify_24h,
            notify_4h=task.notify_4h,
            notification_channels=task.notification_channels,
            published_at=task.published_at,
            closed_at=task.closed_at,
            created_at=task.created_at,
        )


class TaskTransitionResponse(BaseModel):
    """One lifecycle verb's outcome, normalized across verbs:
    where the task landed, whether it is claimable there
    (``TaskService.is_claimable``, the same verdict the claim side
    enforces), and the lifecycle timestamps."""

    task_id: UUID
    status: str
    claimable: bool
    published_at: datetime | None
    closed_at: datetime | None


class TeacherTaskListItemResponse(BaseModel):
    """One workbench list row (spec §41 Task list): card-level facts plus
    lifecycle timestamps, every status including DRAFT. Contract fields
    ride only ``TeacherTaskResponse`` (the detail)."""

    id: UUID
    title: str
    status: str
    task_type: str
    rarity: str
    base_reward_points: int
    deadline_mode: str
    fixed_deadline_at: datetime | None
    duration_minutes: int | None
    published_at: datetime | None
    closed_at: datetime | None
    created_at: datetime

    @classmethod
    def from_view(cls, item: TeacherTaskListItem) -> TeacherTaskListItemResponse:
        return cls(
            id=item.id,
            title=item.title,
            status=item.status,
            task_type=item.task_type,
            rarity=item.rarity,
            base_reward_points=item.base_reward_points,
            deadline_mode=item.deadline_mode,
            fixed_deadline_at=item.fixed_deadline_at,
            duration_minutes=item.duration_minutes,
            published_at=item.published_at,
            closed_at=item.closed_at,
            created_at=item.created_at,
        )


class TeacherTaskListResponse(BaseModel):
    """Offset-paginated workbench page (the documented V1 choice)."""

    items: list[TeacherTaskListItemResponse]
    total: int
    limit: int
    offset: int


class ImportPreviewErrorResponse(BaseModel):
    """One preview error; ``row_number is None`` marks a file-level error."""

    code: str
    message: str
    row_number: int | None = None
    platform: str | None = None
    keyword: str | None = None
    details: dict[str, int] | None = None

    @classmethod
    def from_domain(cls, error: AssignmentImportError) -> ImportPreviewErrorResponse:
        return cls(
            code=error.code.value,
            message=error.message,
            row_number=error.row_number,
            platform=error.platform,
            keyword=error.keyword,
            details=dict(error.details) if error.details is not None else None,
        )


class ImportPreviewResponse(BaseModel):
    """Preview outcome (spec §7.1 step 4): counts + per-row errors. The
    valid rows themselves are NOT echoed — the count is the contract; the
    token names the exact server-stored rows confirm will insert."""

    task_id: UUID
    total_rows: int
    valid_count: int
    error_count: int
    errors: list[ImportPreviewErrorResponse]
    preview_token: str | None
    expires_at: datetime | None

    @classmethod
    def from_domain(cls, preview: AssignmentImportPreview) -> ImportPreviewResponse:
        return cls(
            task_id=preview.task_id,
            total_rows=preview.total_rows,
            valid_count=preview.valid_count,
            error_count=preview.error_count,
            errors=[
                ImportPreviewErrorResponse.from_domain(error)
                for error in preview.errors
            ],
            preview_token=preview.preview_token,
            expires_at=preview.expires_at,
        )


class ImportConfirmResponse(BaseModel):
    """All-or-nothing confirm outcome (spec §7.1 steps 5-6)."""

    task_id: UUID
    inserted: int


class CollaboratorResponse(BaseModel):
    """A granted capability set, in canonical storage order."""

    task_id: UUID
    teacher_id: UUID
    permissions: list[str]


class TaskStatisticsResponse(BaseModel):
    """The Teacher workbench statistics aggregate (spec §41): counts only,
    by construction — no Assignment row material ever rides along."""

    task_id: UUID
    assignments_available: int
    assignments_occupied: int
    assignments_completed: int
    assignments_retired: int
    active_claims: int
    completion_rate: float
    rating: RatingSummaryResponse | None
    submission_counts: dict[str, int]

    @classmethod
    def from_domain(cls, statistics: TaskStatistics) -> TaskStatisticsResponse:
        return cls(
            task_id=statistics.task_id,
            assignments_available=statistics.assignments_available,
            assignments_occupied=statistics.assignments_occupied,
            assignments_completed=statistics.assignments_completed,
            assignments_retired=statistics.assignments_retired,
            active_claims=statistics.active_claims,
            completion_rate=statistics.completion_rate,
            rating=(
                RatingSummaryResponse.from_domain(statistics.rating)
                if statistics.rating is not None
                else None
            ),
            submission_counts=dict(statistics.submission_counts),
        )
