# backend/app/modules/tasks/service.py
"""Task lifecycle use cases: create, publish, pause/resume, close, archive,
edit (spec §6, §6.2, §9.1, §25.1; backend-engineering §4-5, §11).

State machine (spec §6.2, the table is the single authority)::

    DRAFT -> PUBLISHED
    PUBLISHED -> PAUSED | CLOSED
    PAUSED -> PUBLISHED | CLOSED
    CLOSED -> ARCHIVED

Design decisions:

- **Transitions validate by TARGET status.** Each verb carries its target
  (publish/resume -> PUBLISHED, pause -> PAUSED, close -> CLOSED, archive
  -> ARCHIVED) and is admitted iff ``(current -> target)`` is a table
  edge. The verbs are therefore interchangeable on shared targets: a
  PAUSED task may be brought back via either ``publish_task`` or
  ``resume_task``, and ``resume_task`` on a DRAFT behaves as a publish.
  One table, no second verb-specific edge list to drift from it.
- **Every entry into PUBLISHED re-runs publish validation.** The checks
  that do not depend on wall-clock time cannot have changed while paused
  (contract fields are frozen from first publish), but the FIXED-deadline
  window can: a task paused past its cutoff must not silently come back
  as an immediately-unclaimable listing (spec §9.1).
- **Timestamps:** ``published_at`` records FIRST publish and survives
  pause/resume untouched; ``closed_at`` is set once at close. Both come
  from the injected ``Clock`` (§11), never ``datetime.now``.
- **Ownership:** lifecycle verbs and edits are owner-or-Admin
  (``PERMISSION_DENIED`` otherwise); collaborators (T3) carry no
  lifecycle capability in their V1 set, so no seam beyond this check is
  needed yet.
- **Close never touches Claims** (spec §6.2: default keep; §6.2 禁止
  pause/close secretly cancelling claims). Claim disposition on close —
  if ever exposed — belongs to the claim service (T6/T8), not here.
- **Edit rule (V1):** DRAFT is fully editable; PUBLISHED/PAUSED accept
  presentation fields only; CLOSED/ARCHIVED are frozen. See
  ``UpdateTask`` for why contract fields freeze at first publish.
- **Boundaries (plan pre-flight, spec §9.1 "少于 cutoff 时停止"):** blocked
  when remaining time is strictly LESS than ``claim_cutoff_minutes``; at
  exactly the cutoff the task is still claimable/publishable.
- **Unknown task id** raises ``TaskNotFoundError`` carrying the system
  code ``NOT_FOUND`` (404). The registry currently has no business code
  for a missing aggregate; if interfaces.md registers one, remap here
  (doc-first rule) — flagged for the T9 API review.

Transaction shape per backend-engineering §5: one ``SELECT ... FOR
UPDATE``, the invariants, one field mutation block, and exactly one
``commit`` at the end of each use case. Locking against real PostgreSQL
is exercised by the integration suite; these paths are unit-tested with
the duck-typed fake session in tests/unit/tasks/test_task_lifecycle.py.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.rbac import is_admin, is_staff
from app.modules.identity.events import Actor
from app.modules.tasks.enums import DeadlineMode, TaskRarity, TaskStatus, TaskType
from app.modules.tasks.models import Task
from app.modules.tasks.schemas import CreateTask, PublishResult, UpdateTask

# --- frozen value sets -------------------------------------------------------

# Canonical file types (spec §10/§12). Parity with the database CHECK in
# models.py (`_ALLOWED_FILE_TYPES`) is pinned by the unit tests.
SUPPORTED_FILE_TYPES: frozenset[str] = frozenset({"CSV", "XLSX", "SQLITE"})
# Notification channels (interfaces.md §25); parity pinned the same way.
SUPPORTED_NOTIFICATION_CHANNELS: frozenset[str] = frozenset(
    {"SMS", "EMAIL", "IN_APP"}
)
# spec §6: V1 fixes the grace period at 24h with no product entry point.
DEFAULT_GRACE_PERIOD_MINUTES = 1440

# --- transition table (spec §6.2; the brief, verbatim) ------------------------

ALLOWED_TASK_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.DRAFT: {TaskStatus.PUBLISHED},
    TaskStatus.PUBLISHED: {TaskStatus.PAUSED, TaskStatus.CLOSED},
    TaskStatus.PAUSED: {TaskStatus.PUBLISHED, TaskStatus.CLOSED},
    TaskStatus.CLOSED: {TaskStatus.ARCHIVED},
    TaskStatus.ARCHIVED: set(),
}

# UpdateTask fields frozen from first publish on (the V1 edit rule). Any
# field not listed here is a presentation field and stays editable.
_CONTRACT_FIELDS: frozenset[str] = frozenset(
    {
        "base_reward_points",
        "deadline_mode",
        "fixed_deadline_at",
        "duration_minutes",
        "claim_cutoff_minutes",
        "submission_schema",
        "submission_schema_version",
        "allowed_file_types",
        "max_file_size_bytes",
    }
)

# --- messages (§29 envelope text) ---------------------------------------------

_INVALID_TITLE_MESSAGE = "任务标题不能为空，且不超过 255 个字符"
_INVALID_DESCRIPTION_MESSAGE = "任务描述不能为空"
_INVALID_REWARD_MESSAGE = "任务奖励积分必须大于 0"
_UNSUPPORTED_VALUE_MESSAGE = "存在不支持的取值"
_INVALID_DEADLINE_MESSAGE = "截止时间必须携带时区信息"
_PAST_DEADLINE_MESSAGE = "固定截止时间必须晚于当前时间"
_MISSING_FIXED_DEADLINE_MESSAGE = "FIXED 模式必须设置固定截止时间"
_INVALID_DURATION_MESSAGE = "RELATIVE 模式必须设置正的时长（分钟）"
_INVALID_SCHEMA_MESSAGE = "发布前必须配置提交校验 schema 及其版本"
_INVALID_FILE_POLICY_MESSAGE = "发布前必须至少配置一种允许的文件类型"
_INVALID_SIZE_CAP_MESSAGE = "单文件大小上限必须大于 0 且不超过平台上限"
_ILLEGAL_TRANSITION_MESSAGE = "当前任务状态不允许该操作"
_IMMUTABLE_FIELDS_MESSAGE = "该状态下任务字段不可修改"
_TASK_NOT_FOUND_MESSAGE = "任务不存在"
_NOT_OWNER_MESSAGE = "只有任务所有者或管理员可以执行该操作"
_ROLE_DENIED_MESSAGE = "当前角色无权执行该操作"
_CUTOFF_MESSAGE = (
    "距固定截止时间已不足领取窗口，发布后将立即不可领取；"
    "请延后截止时间或调小领取窗口"
)


# --- typed exceptions (router-mapped; T9 seam) --------------------------------


class TaskNotFoundError(BusinessError):
    """No Task row for the id (see module docstring on the code choice)."""

    def __init__(self, task_id: UUID) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            _TASK_NOT_FOUND_MESSAGE,
            status_code=404,
            details={"task_id": str(task_id)},
        )


class IllegalTransitionError(BusinessError):
    """(current -> target) is not an edge in ALLOWED_TASK_TRANSITIONS."""

    def __init__(self, from_status: TaskStatus, to_status: TaskStatus) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _ILLEGAL_TRANSITION_MESSAGE,
            status_code=400,
            details={"from": from_status.value, "to": to_status.value},
        )


class ImmutableTaskFieldError(BusinessError):
    """Fields provided on a task whose status freezes them (V1 edit rule)."""

    def __init__(self, field_names: Sequence[str], status: TaskStatus) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _IMMUTABLE_FIELDS_MESSAGE,
            status_code=400,
            details={"fields": sorted(field_names), "status": status.value},
        )


# --- helpers -------------------------------------------------------------------


def _status(task: Task) -> TaskStatus:
    """Task status as its enum (columns persist the exact member string)."""
    return TaskStatus(task.status)


def _deadline_mode(task: Task) -> DeadlineMode:
    return DeadlineMode(task.deadline_mode)


def _utc(value: datetime) -> datetime:
    """§9.3: comparisons use UTC instants; naive datetimes are rejected."""
    if value.tzinfo is None:
        raise BusinessError(
            ErrorCode.VALIDATION_ERROR,
            _INVALID_DEADLINE_MESSAGE,
            status_code=400,
        )
    return value.astimezone(UTC)


def _member_or[E: StrEnum](enum_cls: type[E], value: E | str) -> E:
    try:
        return enum_cls(value)
    except ValueError:
        raise BusinessError(
            ErrorCode.VALIDATION_ERROR,
            _UNSUPPORTED_VALUE_MESSAGE,
            status_code=400,
            details={"value": str(value)},
        ) from None


def _required_text(value: str, message: str, *, max_length: int | None = None) -> str:
    stripped = value.strip() if isinstance(value, str) else ""
    if not stripped or (max_length is not None and len(stripped) > max_length):
        raise BusinessError(ErrorCode.VALIDATION_ERROR, message, status_code=400)
    return stripped


def _normalize_codes(
    values: Sequence[str], supported: frozenset[str]
) -> list[str]:
    """Upper-case, strip, and de-duplicate closed-set codes.

    Empty input is preserved (allowed_file_types may be an empty DRAFT
    policy); unknown values are rejected here so the database CHECK never
    has to (backend-engineering §6: friendly error before the constraint).
    """
    normalized: list[str] = []
    for raw in values:
        code = raw.strip().upper()
        if code and code not in normalized:
            normalized.append(code)
    unsupported = sorted(set(normalized) - supported)
    if unsupported:
        raise BusinessError(
            ErrorCode.VALIDATION_ERROR,
            _UNSUPPORTED_VALUE_MESSAGE,
            status_code=400,
            details={"unsupported": unsupported},
        )
    return normalized


# --- the service ----------------------------------------------------------------


class TaskService:
    """Task lifecycle and edit use cases (spec §6, §6.2).

    ``max_upload_bytes`` is the platform cap the composition root passes
    from ``settings.max_upload_bytes_default``; injecting the scalar (not
    the whole Settings) keeps the service testable without a deployment
    environment.
    """

    def __init__(self, *, clock: Clock, max_upload_bytes: int) -> None:
        self._clock = clock
        self._max_upload_bytes = max_upload_bytes

    # -- create ---------------------------------------------------------------

    async def create_task(
        self, db: AsyncSession, actor: Actor, command: CreateTask
    ) -> Task:
        """Create a DRAFT task owned by ``actor`` (spec §6; §4 role rule).

        Only TEACHER/ADMIN may create (spec §4); STUDENT gets
        ``PERMISSION_DENIED``. DRAFT-legal incompleteness is accepted;
        values the database would refuse anyway fail fast here.
        """
        if not is_staff(actor.role):
            raise BusinessError(
                ErrorCode.PERMISSION_DENIED,
                _ROLE_DENIED_MESSAGE,
                status_code=403,
            )

        title = _required_text(command.title, _INVALID_TITLE_MESSAGE, max_length=255)
        description = _required_text(command.description, _INVALID_DESCRIPTION_MESSAGE)
        if command.base_reward_points <= 0:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR, _INVALID_REWARD_MESSAGE, status_code=400
            )
        if command.duration_minutes is not None and command.duration_minutes <= 0:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR, _INVALID_DURATION_MESSAGE, status_code=400
            )
        if command.claim_cutoff_minutes < 0:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _UNSUPPORTED_VALUE_MESSAGE,
                status_code=400,
                details={"field": "claim_cutoff_minutes"},
            )
        self._validate_size_cap(command.max_file_size_bytes)
        if command.submission_schema is not None and not command.submission_schema:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR, _INVALID_SCHEMA_MESSAGE, status_code=400
            )
        if (
            command.submission_schema_version is not None
            and command.submission_schema_version < 1
        ):
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR, _INVALID_SCHEMA_MESSAGE, status_code=400
            )

        task = Task(
            owner_teacher_id=actor.user_id,
            title=title,
            description=description,
            task_type=_member_or(TaskType, command.task_type),
            rarity=_member_or(TaskRarity, command.rarity),
            base_reward_points=command.base_reward_points,
            status=TaskStatus.DRAFT,
            deadline_mode=_member_or(DeadlineMode, command.deadline_mode),
            fixed_deadline_at=(
                _utc(command.fixed_deadline_at)
                if command.fixed_deadline_at is not None
                else None
            ),
            duration_minutes=command.duration_minutes,
            grace_period_minutes=DEFAULT_GRACE_PERIOD_MINUTES,
            claim_cutoff_minutes=command.claim_cutoff_minutes,
            submission_schema=(
                dict(command.submission_schema)
                if command.submission_schema is not None
                else None
            ),
            submission_schema_version=command.submission_schema_version,
            allowed_file_types=_normalize_codes(
                command.allowed_file_types, SUPPORTED_FILE_TYPES
            ),
            max_file_size_bytes=command.max_file_size_bytes,
            notify_24h=command.notify_24h,
            notify_4h=command.notify_4h,
            notification_channels=_normalize_codes(
                command.notification_channels, SUPPORTED_NOTIFICATION_CHANNELS
            ),
        )
        db.add(task)
        await db.commit()
        return task

    # -- lifecycle verbs --------------------------------------------------------

    async def publish_task(
        self, db: AsyncSession, actor: Actor, task_id: UUID
    ) -> PublishResult:
        """Move a task into PUBLISHED (spec §6.2) after publish validation.

        Also the resume path's twin: any table-legal entry into PUBLISHED
        runs the same validation (see module docstring).
        """
        task = await self._transition(db, actor, task_id, TaskStatus.PUBLISHED)
        return PublishResult(
            task_id=task.id,
            status=_status(task),
            published_at=task.published_at or self._clock.now(),
            claimable=self.is_claimable(task),
        )

    async def pause_task(
        self, db: AsyncSession, actor: Actor, task_id: UUID
    ) -> Task:
        """PUBLISHED -> PAUSED: stop NEW claims, keep existing ones (§6.2)."""
        return await self._transition(db, actor, task_id, TaskStatus.PAUSED)

    async def resume_task(
        self, db: AsyncSession, actor: Actor, task_id: UUID
    ) -> Task:
        """PAUSED -> PUBLISHED (re-validated; see module docstring)."""
        return await self._transition(db, actor, task_id, TaskStatus.PUBLISHED)

    async def close_task(
        self, db: AsyncSession, actor: Actor, task_id: UUID
    ) -> Task:
        """PUBLISHED/PAUSED -> CLOSED: no new claims; existing claims keep
        running (default disposition — spec §6.2); ``closed_at`` is set."""
        return await self._transition(db, actor, task_id, TaskStatus.CLOSED)

    async def archive_task(
        self, db: AsyncSession, actor: Actor, task_id: UUID
    ) -> Task:
        """CLOSED -> ARCHIVED: history only (spec §6.2)."""
        return await self._transition(db, actor, task_id, TaskStatus.ARCHIVED)

    # -- edit --------------------------------------------------------------------

    async def update_task(
        self, db: AsyncSession, actor: Actor, task_id: UUID, command: UpdateTask
    ) -> Task:
        """Apply a partial update under the V1 edit rule (spec §6.2).

        DRAFT: every field; PUBLISHED/PAUSED: presentation fields only;
        CLOSED/ARCHIVED: nothing. See ``UpdateTask`` for the field split
        and the why (claim snapshots).
        """
        task = await self._locked_task(db, task_id)
        self._require_owner_or_admin(task, actor)
        status = _status(task)

        provided = [
            field.name
            for field in dataclasses.fields(command)
            if getattr(command, field.name) is not None
        ]
        if status in (TaskStatus.CLOSED, TaskStatus.ARCHIVED):
            if provided:
                raise ImmutableTaskFieldError(provided, status)
            return task  # a no-op edit on a frozen task changes nothing
        if status in (TaskStatus.PUBLISHED, TaskStatus.PAUSED):
            contract_provided = [f for f in provided if f in _CONTRACT_FIELDS]
            if contract_provided:
                raise ImmutableTaskFieldError(contract_provided, status)

        # Presentation fields (any live status).
        if command.title is not None:
            task.title = _required_text(
                command.title, _INVALID_TITLE_MESSAGE, max_length=255
            )
        if command.description is not None:
            task.description = _required_text(
                command.description, _INVALID_DESCRIPTION_MESSAGE
            )
        if command.notify_24h is not None:
            task.notify_24h = command.notify_24h
        if command.notify_4h is not None:
            task.notify_4h = command.notify_4h
        if command.notification_channels is not None:
            task.notification_channels = _normalize_codes(
                command.notification_channels, SUPPORTED_NOTIFICATION_CHANNELS
            )

        # Contract fields (DRAFT only from here on).
        if command.base_reward_points is not None:
            if command.base_reward_points <= 0:
                raise BusinessError(
                    ErrorCode.VALIDATION_ERROR,
                    _INVALID_REWARD_MESSAGE,
                    status_code=400,
                )
            task.base_reward_points = command.base_reward_points
        if command.deadline_mode is not None:
            task.deadline_mode = _member_or(DeadlineMode, command.deadline_mode)
        if command.fixed_deadline_at is not None:
            task.fixed_deadline_at = _utc(command.fixed_deadline_at)
        if command.duration_minutes is not None:
            if command.duration_minutes <= 0:
                raise BusinessError(
                    ErrorCode.VALIDATION_ERROR,
                    _INVALID_DURATION_MESSAGE,
                    status_code=400,
                )
            task.duration_minutes = command.duration_minutes
        if command.claim_cutoff_minutes is not None:
            if command.claim_cutoff_minutes < 0:
                raise BusinessError(
                    ErrorCode.VALIDATION_ERROR,
                    _UNSUPPORTED_VALUE_MESSAGE,
                    status_code=400,
                    details={"field": "claim_cutoff_minutes"},
                )
            task.claim_cutoff_minutes = command.claim_cutoff_minutes
        if command.submission_schema is not None:
            if not command.submission_schema:
                raise BusinessError(
                    ErrorCode.VALIDATION_ERROR,
                    _INVALID_SCHEMA_MESSAGE,
                    status_code=400,
                )
            task.submission_schema = dict(command.submission_schema)
        if command.submission_schema_version is not None:
            if command.submission_schema_version < 1:
                raise BusinessError(
                    ErrorCode.VALIDATION_ERROR,
                    _INVALID_SCHEMA_MESSAGE,
                    status_code=400,
                )
            task.submission_schema_version = command.submission_schema_version
        if command.allowed_file_types is not None:
            task.allowed_file_types = _normalize_codes(
                command.allowed_file_types, SUPPORTED_FILE_TYPES
            )
        if command.max_file_size_bytes is not None:
            self._validate_size_cap(command.max_file_size_bytes)
            task.max_file_size_bytes = command.max_file_size_bytes

        await db.commit()
        return task

    # -- claimability -------------------------------------------------------------

    def is_claimable(self, task: Task, *, now: datetime | None = None) -> bool:
        """The claim gate the claim service (T6) will reuse (spec §6.2, §9.1).

        Only PUBLISHED is claimable — DRAFT is invisible, PAUSED/CLOSED
        stop new claims. FIXED additionally needs remaining time >= the
        claim cutoff (strictly-less-than blocks, matching the publish
        boundary); RELATIVE deadlines are computed per claim, so PUBLISHED
        alone suffices. A naive ``fixed_deadline_at`` cannot be compared
        as an instant and reports not-claimable rather than raising; the
        write paths reject such values anyway.
        """
        if _status(task) is not TaskStatus.PUBLISHED:
            return False
        if _deadline_mode(task) is DeadlineMode.RELATIVE:
            return True
        if task.fixed_deadline_at is None or task.fixed_deadline_at.tzinfo is None:
            return False
        current = self._clock.now() if now is None else now
        cutoff = timedelta(minutes=task.claim_cutoff_minutes or 0)
        return task.fixed_deadline_at.astimezone(UTC) >= current + cutoff

    # -- internals ----------------------------------------------------------------

    async def _transition(
        self, db: AsyncSession, actor: Actor, task_id: UUID, target: TaskStatus
    ) -> Task:
        """Lock, authorize, validate the table edge, mutate, commit once."""
        task = await self._locked_task(db, task_id)
        self._require_owner_or_admin(task, actor)
        self._require_transition(task, target)

        if target is TaskStatus.PUBLISHED:
            now = self._clock.now()
            self._validate_publishable(task, now)
            if task.published_at is None:  # first publish; pause/resume keeps it
                task.published_at = now
        elif target is TaskStatus.CLOSED:
            task.closed_at = self._clock.now()

        task.status = target
        await db.commit()
        return task

    async def _locked_task(self, db: AsyncSession, task_id: UUID) -> Task:
        """The Task row under FOR UPDATE, or TaskNotFoundError."""
        task = await db.scalar(
            select(Task).where(Task.id == task_id).with_for_update()
        )
        if task is None:
            raise TaskNotFoundError(task_id)
        return task

    @staticmethod
    def _require_owner_or_admin(task: Task, actor: Actor) -> None:
        if actor.user_id != task.owner_teacher_id and not is_admin(actor.role):
            raise BusinessError(
                ErrorCode.PERMISSION_DENIED,
                _NOT_OWNER_MESSAGE,
                status_code=403,
            )

    @staticmethod
    def _require_transition(task: Task, target: TaskStatus) -> None:
        current = _status(task)
        if target not in ALLOWED_TASK_TRANSITIONS[current]:
            raise IllegalTransitionError(current, target)

    def _validate_size_cap(self, max_file_size_bytes: int) -> None:
        if max_file_size_bytes <= 0 or max_file_size_bytes > self._max_upload_bytes:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _INVALID_SIZE_CAP_MESSAGE,
                status_code=400,
            )

    def _validate_publishable(self, task: Task, now: datetime) -> None:
        """The publish gate (spec §6.2 + §9.1); runs on every entry into
        PUBLISHED so a hand-planted or stale row faces the same rules as
        one created through ``create_task``.

        Order: reward -> deadline policy -> submission schema -> file
        policy -> size cap (the brief's order).
        """
        if task.base_reward_points is None or task.base_reward_points <= 0:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR, _INVALID_REWARD_MESSAGE, status_code=400
            )

        if _deadline_mode(task) is DeadlineMode.FIXED:
            if task.fixed_deadline_at is None:
                raise BusinessError(
                    ErrorCode.VALIDATION_ERROR,
                    _MISSING_FIXED_DEADLINE_MESSAGE,
                    status_code=400,
                )
            deadline = _utc(task.fixed_deadline_at)
            if deadline <= now:
                raise BusinessError(
                    ErrorCode.VALIDATION_ERROR,
                    _PAST_DEADLINE_MESSAGE,
                    status_code=400,
                )
            cutoff = timedelta(minutes=task.claim_cutoff_minutes or 0)
            if deadline < now + cutoff:
                # spec §9.1: block with a clear error instead of going
                # live with (almost) no claimable time left.
                raise BusinessError(
                    ErrorCode.TASK_NOT_CLAIMABLE,
                    _CUTOFF_MESSAGE,
                    status_code=400,
                    details={"claim_cutoff_minutes": task.claim_cutoff_minutes},
                )
        else:
            if task.duration_minutes is None or task.duration_minutes <= 0:
                raise BusinessError(
                    ErrorCode.VALIDATION_ERROR,
                    _INVALID_DURATION_MESSAGE,
                    status_code=400,
                )

        if not task.submission_schema:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR, _INVALID_SCHEMA_MESSAGE, status_code=400
            )
        if task.submission_schema_version is None or task.submission_schema_version < 1:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR, _INVALID_SCHEMA_MESSAGE, status_code=400
            )

        if not task.allowed_file_types:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _INVALID_FILE_POLICY_MESSAGE,
                status_code=400,
            )
        unsupported = sorted(set(task.allowed_file_types) - SUPPORTED_FILE_TYPES)
        if unsupported:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _UNSUPPORTED_VALUE_MESSAGE,
                status_code=400,
                details={"unsupported": unsupported},
            )

        self._validate_size_cap(task.max_file_size_bytes)
