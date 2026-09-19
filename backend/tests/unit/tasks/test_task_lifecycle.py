# backend/tests/unit/tasks/test_task_lifecycle.py
"""Unit tests for the Task lifecycle service (spec §6.2, §9.1; plan 03 T2).

Strategy (documented per the task brief): the lifecycle is transition-table
plus validation logic, so these are unit tests over a duck-typed fake
session — no PostgreSQL. `FakeSession` implements exactly the AsyncSession
surface `TaskService` uses (`add` / `flush` / `commit` / `scalar` on a
single-table `select(Task).where(...).with_for_update()`), hands back
in-memory `Task` rows, mints `id` / `created_at` the way the PostgreSQL
server defaults would, and records FOR UPDATE lock requests. Real-database
locking and constraint behavior stays in the integration suite (task 1).

Timestamps come from a mutable injected clock (`FrozenClock` is immutable
by design), so the §9.1 publish-cutoff boundary
(deadline vs now + claim_cutoff_minutes) is pinned exactly, matching the
plan's pre-flight decision: blocked only when remaining time is strictly
LESS than the cutoff.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.tasks.enums import ClaimStatus, DeadlineMode, TaskStatus
from app.modules.tasks.models import (
    _ALLOWED_FILE_TYPES,
    _NOTIFICATION_CHANNELS,
    AssignmentClaim,
    Task,
)
from app.modules.tasks.schemas import (
    CreateTask,
    PublishResult,
    TaskPublic,
    UpdateTask,
)
from app.modules.tasks.service import (
    ALLOWED_TASK_TRANSITIONS,
    SUPPORTED_FILE_TYPES,
    SUPPORTED_NOTIFICATION_CHANNELS,
    IllegalTransitionError,
    ImmutableTaskFieldError,
    TaskNotFoundError,
    TaskService,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
MAX_UPLOAD_BYTES = 1024
# The brief's transition table, verbatim, for the table-parity test.
SPEC_TRANSITIONS = {
    TaskStatus.DRAFT: {TaskStatus.PUBLISHED},
    TaskStatus.PUBLISHED: {TaskStatus.PAUSED, TaskStatus.CLOSED},
    TaskStatus.PAUSED: {TaskStatus.PUBLISHED, TaskStatus.CLOSED},
    TaskStatus.CLOSED: {TaskStatus.ARCHIVED},
    TaskStatus.ARCHIVED: set(),
}


def _actor(role: Role, user_id: UUID | None = None) -> Actor:
    return Actor(user_id=user_id or uuid4(), role=role)


OWNER = _actor(Role.TEACHER)
STRANGER_TEACHER = _actor(Role.TEACHER)
ADMIN = _actor(Role.ADMIN)
STUDENT = _actor(Role.STUDENT)


class FakeSession:
    """Duck-typed AsyncSession for TaskService (see module docstring)."""

    def __init__(self, *tasks: Task) -> None:
        self.tasks: dict[UUID, Task] = {t.id: t for t in tasks}
        self._pending: list[Task] = []
        self.commits = 0
        self.locked_ids: list[UUID] = []

    def add(self, task: Task) -> None:
        self._pending.append(task)

    async def flush(self) -> None:
        # Server defaults (gen_random_uuid / now) as they would land in PG.
        for task in self._pending:
            if task.id is None:
                task.id = uuid4()
            if task.created_at is None:
                task.created_at = NOW
            self.tasks[task.id] = task
        self._pending.clear()

    async def commit(self) -> None:
        await self.flush()
        self.commits += 1

    async def scalar(self, stmt: Any) -> Task | None:
        assert stmt.column_descriptions[0]["entity"] is Task
        task_id = stmt.whereclause.right.value
        assert isinstance(task_id, UUID)
        if stmt._for_update_arg is not None:
            self.locked_ids.append(task_id)
        return self.tasks.get(task_id)


def command(**overrides: Any) -> CreateTask:
    """A fully publish-ready RELATIVE command; overrides patch any field."""
    values: dict[str, Any] = {
        "title": "校园二手书帖子采集",
        "description": "按关键词采集小红书二手书相关帖子并提交表格。",
        "base_reward_points": 100,
        "deadline_mode": DeadlineMode.RELATIVE,
        "duration_minutes": 7200,
        "allowed_file_types": ("csv",),
        "max_file_size_bytes": 512,
        "submission_schema": {"columns": ["post_url", "content"]},
        "submission_schema_version": 1,
    }
    values.update(overrides)
    return CreateTask(**values)


def planted_task(
    *, status: TaskStatus = TaskStatus.DRAFT, owner: Actor = OWNER, **overrides: Any
) -> Task:
    """A Task row hand-built in fully publishable configuration.

    Planting (instead of going through create_task) lets publish-validation
    tests exercise rows that the service's own create path would already
    have rejected — publish must re-validate regardless of row history.
    """
    values: dict[str, Any] = {
        "id": uuid4(),
        "owner_teacher_id": owner.user_id,
        "title": "已就绪任务",
        "description": "desc",
        "task_type": "DATA_CRAWL",
        "rarity": "NORMAL",
        "base_reward_points": 100,
        "status": status,
        "deadline_mode": "RELATIVE",
        "duration_minutes": 7200,
        "grace_period_minutes": 1440,
        "claim_cutoff_minutes": 240,
        "submission_schema": {"columns": ["post_url"]},
        "submission_schema_version": 1,
        "allowed_file_types": ["CSV"],
        "max_file_size_bytes": 512,
        "notify_24h": True,
        "notify_4h": True,
        "notification_channels": ["SMS", "EMAIL", "IN_APP"],
        "created_at": NOW,
    }
    values.update(overrides)
    return Task(**values)


def planted_claim(task: Task) -> AssignmentClaim:
    return AssignmentClaim(
        assignment_id=uuid4(),
        task_id=task.id,
        user_id=uuid4(),
        status=ClaimStatus.CLAIMED,
        claimed_at=NOW,
        deadline_at=NOW + timedelta(hours=5),
        grace_deadline_at=NOW + timedelta(hours=29),
        reward_policy_snapshot={"tiers": []},
        base_reward_points_snapshot=100,
        submission_schema_version=1,
        reward_lock_status="NONE",
    )


@dataclass
class MutableClock:
    """FrozenClock is immutable by design; lifecycle tests advance time."""

    current: datetime

    def now(self) -> datetime:
        return self.current.astimezone(UTC)


@pytest.fixture()
def clock() -> MutableClock:
    return MutableClock(NOW)


@pytest.fixture()
def service(clock: MutableClock) -> TaskService:
    return TaskService(clock=clock, max_upload_bytes=MAX_UPLOAD_BYTES)


# --- transition table ------------------------------------------------------


def test_transition_table_matches_spec_exactly() -> None:
    assert ALLOWED_TASK_TRANSITIONS == SPEC_TRANSITIONS


def test_supported_value_sets_match_model_checks() -> None:
    # The service validates against these sets; the database CHECK
    # constraints in models.py pin the same values. Drift = rejected rows.
    assert frozenset(_ALLOWED_FILE_TYPES) == SUPPORTED_FILE_TYPES
    assert frozenset(_NOTIFICATION_CHANNELS) == SUPPORTED_NOTIFICATION_CHANNELS


VERBS: dict[str, TaskStatus] = {
    "publish": TaskStatus.PUBLISHED,
    "pause": TaskStatus.PAUSED,
    "resume": TaskStatus.PUBLISHED,
    "close": TaskStatus.CLOSED,
    "archive": TaskStatus.ARCHIVED,
}


@pytest.mark.parametrize(
    ("from_status", "verb"),
    [(from_status, verb) for from_status in TaskStatus for verb in VERBS],
    ids=[f"{fs.value}-{v}" for fs in TaskStatus for v in VERBS],
)
async def test_every_transition_pair_enforced_via_service(
    service: TaskService, from_status: TaskStatus, verb: str
) -> None:
    """All 25 (status, verb) pairs: success iff target is in the table.

    The table is validated by TARGET status: each verb carries its target
    and `TaskService` checks (current -> target) membership, so the state
    table is the single authority (publish and resume both target
    PUBLISHED; either may traverse any table-legal edge into it).
    """
    task = planted_task(status=from_status)
    db = FakeSession(task)
    target = VERBS[verb]
    allowed = target in ALLOWED_TASK_TRANSITIONS[from_status]

    method = getattr(service, f"{verb}_task")
    if allowed:
        result = await method(db, OWNER, task.id)
        assert task.status == target
        assert db.commits == 1
        if result is not None and not isinstance(result, PublishResult):
            assert result is task
    else:
        with pytest.raises(IllegalTransitionError) as excinfo:
            await method(db, OWNER, task.id)
        assert excinfo.value.code == ErrorCode.VALIDATION_ERROR
        assert excinfo.value.details["from"] == from_status
        assert excinfo.value.details["to"] == target
        assert task.status == from_status  # unchanged
        assert db.commits == 0


async def test_draft_to_archived_rejected(service: TaskService) -> None:
    task = planted_task()
    db = FakeSession(task)
    with pytest.raises(IllegalTransitionError):
        await service.archive_task(db, OWNER, task.id)
    assert task.status == TaskStatus.DRAFT
    assert db.commits == 0


async def test_transitions_lock_the_row_for_update(
    service: TaskService,
) -> None:
    task = await service.create_task(db := FakeSession(), OWNER, command())
    await service.publish_task(db, OWNER, task.id)
    await service.pause_task(db, OWNER, task.id)
    assert db.locked_ids == [task.id, task.id]


# --- full lifecycle chain --------------------------------------------------


async def test_full_lifecycle_chain_with_timestamps(
    service: TaskService, clock: MutableClock
) -> None:
    db = FakeSession()
    task = await service.create_task(db, OWNER, command())
    assert task.status == TaskStatus.DRAFT
    assert task.published_at is None
    assert task.closed_at is None

    result = await service.publish_task(db, OWNER, task.id)
    assert isinstance(result, PublishResult)
    assert result.task_id == task.id
    assert result.status == TaskStatus.PUBLISHED
    assert result.published_at == NOW
    assert result.claimable is True
    assert task.published_at == NOW
    assert service.is_claimable(task) is True

    clock.current = NOW + timedelta(hours=1)
    await service.pause_task(db, OWNER, task.id)
    assert task.status == TaskStatus.PAUSED
    assert service.is_claimable(task) is False

    clock.current = NOW + timedelta(hours=2)
    await service.resume_task(db, OWNER, task.id)
    assert task.status == TaskStatus.PUBLISHED
    assert service.is_claimable(task) is True
    # First-publish time is history: pause/resume must not restamp it.
    assert task.published_at == NOW

    clock.current = NOW + timedelta(hours=3)
    await service.close_task(db, OWNER, task.id)
    assert task.status == TaskStatus.CLOSED
    assert task.closed_at == NOW + timedelta(hours=3)
    assert service.is_claimable(task) is False

    await service.archive_task(db, OWNER, task.id)
    assert task.status == TaskStatus.ARCHIVED
    assert service.is_claimable(task) is False
    # closed_at survives archival
    assert task.closed_at == NOW + timedelta(hours=3)


async def test_pause_and_close_keep_existing_claims(service: TaskService) -> None:
    """PAUSED/CLOSED stop NEW claims and never rewrite existing ones."""
    db = FakeSession()
    task = await service.create_task(db, OWNER, command())
    await service.publish_task(db, OWNER, task.id)
    claim = planted_claim(task)
    claim_deadline = claim.deadline_at

    await service.pause_task(db, OWNER, task.id)
    await service.resume_task(db, OWNER, task.id)
    await service.close_task(db, OWNER, task.id)
    assert claim.status == ClaimStatus.CLAIMED
    assert claim.deadline_at == claim_deadline
    assert claim.base_reward_points_snapshot == 100


# --- publish validation: deadline policy ------------------------------------


async def test_publish_fixed_without_deadline_rejected(
    service: TaskService,
) -> None:
    task = planted_task(
        deadline_mode="FIXED", fixed_deadline_at=None, duration_minutes=None
    )
    db = FakeSession(task)
    with pytest.raises(BusinessError) as excinfo:
        await service.publish_task(db, OWNER, task.id)
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR
    assert task.status == TaskStatus.DRAFT
    assert db.commits == 0


async def test_publish_fixed_deadline_in_past_rejected(
    service: TaskService,
) -> None:
    task = planted_task(
        deadline_mode="FIXED",
        fixed_deadline_at=NOW - timedelta(minutes=1),
        duration_minutes=None,
    )
    with pytest.raises(BusinessError) as excinfo:
        await service.publish_task(FakeSession(task), OWNER, task.id)
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR


async def test_publish_fixed_deadline_naive_rejected(service: TaskService) -> None:
    task = planted_task(
        deadline_mode="FIXED",
        fixed_deadline_at=NOW.replace(tzinfo=None) + timedelta(days=2),
        duration_minutes=None,
    )
    with pytest.raises(BusinessError) as excinfo:
        await service.publish_task(FakeSession(task), OWNER, task.id)
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR


@pytest.mark.parametrize("minutes_ahead", [60, 239])
async def test_publish_fixed_within_claim_cutoff_blocked(
    service: TaskService, minutes_ahead: int
) -> None:
    """§9.1: publishing with less than the cutoff remaining must be a clear
    block, not a live task with negative remaining time."""
    task = planted_task(
        deadline_mode="FIXED",
        fixed_deadline_at=NOW + timedelta(minutes=minutes_ahead),
        duration_minutes=None,
    )
    db = FakeSession(task)
    with pytest.raises(BusinessError) as excinfo:
        await service.publish_task(db, OWNER, task.id)
    assert excinfo.value.code == ErrorCode.TASK_NOT_CLAIMABLE
    assert task.status == TaskStatus.DRAFT
    assert db.commits == 0


async def test_publish_fixed_exactly_at_cutoff_allowed(service: TaskService) -> None:
    """Boundary (plan pre-flight): blocked only when remaining < cutoff."""
    task = planted_task(
        deadline_mode="FIXED",
        fixed_deadline_at=NOW + timedelta(minutes=240),
        duration_minutes=None,
    )
    result = await service.publish_task(FakeSession(task), OWNER, task.id)
    assert result.claimable is True


@pytest.mark.parametrize(
    ("verb", "advance_minutes", "expected_code"),
    [
        # Inside the cutoff (239 < 240 minutes remain) on either verb.
        ("resume", 121, ErrorCode.TASK_NOT_CLAIMABLE),
        ("publish", 121, ErrorCode.TASK_NOT_CLAIMABLE),
        # Deadline itself has passed -> the earlier past-deadline check.
        ("resume", 400, ErrorCode.VALIDATION_ERROR),
        ("publish", 400, ErrorCode.VALIDATION_ERROR),
    ],
    ids=[
        "resume-inside-cutoff",
        "publish-inside-cutoff",
        "resume-past-deadline",
        "publish-past-deadline",
    ],
)
async def test_entry_into_published_revalidates_stale_fixed_deadline(
    service: TaskService,
    clock: MutableClock,
    verb: str,
    advance_minutes: int,
    expected_code: ErrorCode,
) -> None:
    """Regression (fix round 1): EVERY entry into PUBLISHED re-runs publish
    validation, so a paused FIXED task whose deadline window went stale
    must not come back live — on either verb, since publish and resume
    share the target check (spec §9.1: no negative-remaining-time
    listings). Without this, a refactor letting resume skip validation
    would keep the rest of the suite green.
    """
    db = FakeSession()
    task = await service.create_task(
        db,
        OWNER,
        command(
            deadline_mode=DeadlineMode.FIXED,
            fixed_deadline_at=NOW + timedelta(minutes=360),
            duration_minutes=None,
        ),
    )
    await service.publish_task(db, OWNER, task.id)
    await service.pause_task(db, OWNER, task.id)
    assert service.is_claimable(task) is False  # PAUSED stops new claims
    assert db.commits == 3  # create + publish + pause

    clock.current = NOW + timedelta(minutes=advance_minutes)
    with pytest.raises(BusinessError) as excinfo:
        await getattr(service, f"{verb}_task")(db, OWNER, task.id)
    assert excinfo.value.code == expected_code
    assert task.status == TaskStatus.PAUSED  # unchanged; no commit landed
    assert db.commits == 3
    assert db.locked_ids[-1] == task.id  # the blocked attempt did lock


@pytest.mark.parametrize("duration", [None, 0, -30])
async def test_publish_relative_requires_positive_duration(
    service: TaskService, duration: int | None
) -> None:
    task = planted_task(duration_minutes=duration)
    with pytest.raises(BusinessError) as excinfo:
        await service.publish_task(FakeSession(task), OWNER, task.id)
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR
    assert task.status == TaskStatus.DRAFT


# --- publish validation: reward / schema / file policy ----------------------


async def test_publish_rejects_non_positive_reward(service: TaskService) -> None:
    task = planted_task(base_reward_points=0)
    with pytest.raises(BusinessError) as excinfo:
        await service.publish_task(FakeSession(task), OWNER, task.id)
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR


@pytest.mark.parametrize(
    ("overrides", "expected_detail"),
    [
        ({"submission_schema": None}, "schema"),
        ({"submission_schema": {}}, "schema"),
        ({"submission_schema_version": None}, "版本"),
        ({"submission_schema_version": 0}, "版本"),
    ],
)
async def test_publish_requires_submission_schema_and_version(
    service: TaskService, overrides: dict[str, Any], expected_detail: str
) -> None:
    task = planted_task(**overrides)
    with pytest.raises(BusinessError) as excinfo:
        await service.publish_task(FakeSession(task), OWNER, task.id)
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR
    assert expected_detail in str(excinfo.value.message) + str(
        excinfo.value.details or {}
    )


@pytest.mark.parametrize("file_types", [[], ["PDF"], ["CSV", "DOCX"]])
async def test_publish_rejects_empty_or_unsupported_file_types(
    service: TaskService, file_types: list[str]
) -> None:
    task = planted_task(allowed_file_types=file_types)
    with pytest.raises(BusinessError) as excinfo:
        await service.publish_task(FakeSession(task), OWNER, task.id)
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR


@pytest.mark.parametrize("size", [0, -1, MAX_UPLOAD_BYTES + 1])
async def test_publish_rejects_invalid_size_cap(
    service: TaskService, size: int
) -> None:
    task = planted_task(max_file_size_bytes=size)
    with pytest.raises(BusinessError) as excinfo:
        await service.publish_task(FakeSession(task), OWNER, task.id)
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR


# --- create_task -------------------------------------------------------------


async def test_create_task_defaults_and_normalization(
    service: TaskService,
) -> None:
    db = FakeSession()
    task = await service.create_task(db, OWNER, command())
    assert task.status == TaskStatus.DRAFT
    assert task.owner_teacher_id == OWNER.user_id
    assert task.published_at is None
    assert task.grace_period_minutes == 1440  # spec §6: V1 fixed
    assert task.claim_cutoff_minutes == 240  # spec §6: FIXED default
    assert task.notify_24h is True  # spec §25.1: explicit-with-default
    assert task.notify_4h is True
    assert task.notification_channels == ["SMS", "EMAIL", "IN_APP"]
    assert task.allowed_file_types == ["CSV"]  # normalized to DB values
    assert task.submission_schema == {"columns": ["post_url", "content"]}
    assert task.submission_schema_version == 1
    assert db.commits == 1
    assert task.id is not None


async def test_create_task_allows_incomplete_draft_config(
    service: TaskService,
) -> None:
    """DRAFT rows may be incomplete; publish is the completeness gate."""
    db = FakeSession()
    task = await service.create_task(
        db,
        OWNER,
        command(
            deadline_mode=DeadlineMode.FIXED,
            fixed_deadline_at=None,
            duration_minutes=None,
            allowed_file_types=(),
            submission_schema=None,
            submission_schema_version=None,
        ),
    )
    assert task.status == TaskStatus.DRAFT
    assert task.allowed_file_types == []
    assert task.submission_schema is None


async def test_create_task_student_denied(service: TaskService) -> None:
    db = FakeSession()
    with pytest.raises(BusinessError) as excinfo:
        await service.create_task(db, STUDENT, command())
    assert excinfo.value.code == ErrorCode.PERMISSION_DENIED
    assert excinfo.value.status_code == 403
    assert db.commits == 0
    assert not db.tasks


async def test_create_task_admin_allowed(service: TaskService) -> None:
    task = await service.create_task(FakeSession(), ADMIN, command())
    assert task.owner_teacher_id == ADMIN.user_id


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_reward_points": 0},
        {"base_reward_points": -5},
        {"title": "   "},
        {"title": "x" * 256},
        {"description": ""},
        {"allowed_file_types": ("pdf",)},
        {"max_file_size_bytes": 0},
        {"max_file_size_bytes": MAX_UPLOAD_BYTES + 1},
        {"duration_minutes": 0},
        {"duration_minutes": -1},
        {"claim_cutoff_minutes": -1},
        {"notification_channels": ("PUSH",)},
        {"deadline_mode": "WHENEVER"},
        {"task_type": "PHOTO_SHOOT"},
        {"rarity": "MYTHIC"},
        {
            "deadline_mode": DeadlineMode.FIXED,
            "fixed_deadline_at": NOW.replace(tzinfo=None),
            "duration_minutes": None,
        },
    ],
)
async def test_create_task_rejects_invalid_fields(
    service: TaskService, overrides: dict[str, Any]
) -> None:
    db = FakeSession()
    with pytest.raises(BusinessError) as excinfo:
        await service.create_task(db, OWNER, command(**overrides))
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR
    assert excinfo.value.status_code == 400
    assert db.commits == 0
    assert not db.tasks


# --- editing: the published-task edit rule -----------------------------------


async def test_published_reward_edit_blocked(service: TaskService) -> None:
    """Spec §6.2: no retroactive base_reward_points effect on claims; V1
    blocks the field outright once published (claims snapshot anyway)."""
    db = FakeSession()
    task = await service.create_task(db, OWNER, command())
    await service.publish_task(db, OWNER, task.id)
    with pytest.raises(ImmutableTaskFieldError) as excinfo:
        await service.update_task(
            db, OWNER, task.id, UpdateTask(base_reward_points=200)
        )
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR
    assert excinfo.value.details["fields"] == ["base_reward_points"]
    assert task.base_reward_points == 100
    assert db.commits == 2  # create + publish; the rejected edit commits none


@pytest.mark.parametrize(
    "update",
    [
        UpdateTask(deadline_mode=DeadlineMode.FIXED),
        UpdateTask(fixed_deadline_at=NOW + timedelta(days=3)),
        UpdateTask(duration_minutes=60),
        UpdateTask(claim_cutoff_minutes=10),
        UpdateTask(submission_schema={"columns": ["other"]}),
        UpdateTask(submission_schema_version=2),
        UpdateTask(allowed_file_types=("XLSX",)),
        UpdateTask(max_file_size_bytes=256),
    ],
)
async def test_published_contract_fields_blocked(
    service: TaskService, update: UpdateTask
) -> None:
    db = FakeSession()
    task = await service.create_task(db, OWNER, command())
    await service.publish_task(db, OWNER, task.id)
    with pytest.raises(ImmutableTaskFieldError):
        await service.update_task(db, OWNER, task.id, update)
    assert db.commits == 2  # create + publish


async def test_published_presentation_fields_editable(service: TaskService) -> None:
    db = FakeSession()
    task = await service.create_task(db, OWNER, command())
    await service.publish_task(db, OWNER, task.id)
    updated = await service.update_task(
        db,
        OWNER,
        task.id,
        UpdateTask(
            title="新标题",
            description="新描述",
            notify_24h=False,
            notify_4h=False,
            notification_channels=("in_app",),
        ),
    )
    assert updated.title == "新标题"
    assert updated.notify_24h is False
    assert updated.notification_channels == ["IN_APP"]
    assert db.commits == 3  # create + publish + edit


async def test_draft_contract_fields_editable(service: TaskService) -> None:
    db = FakeSession()
    task = await service.create_task(db, OWNER, command())
    await service.update_task(
        db,
        OWNER,
        task.id,
        UpdateTask(base_reward_points=250, deadline_mode=DeadlineMode.FIXED),
    )
    assert task.base_reward_points == 250
    assert task.deadline_mode == DeadlineMode.FIXED


async def test_paused_follows_published_edit_rule(service: TaskService) -> None:
    db = FakeSession()
    task = await service.create_task(db, OWNER, command())
    await service.publish_task(db, OWNER, task.id)
    await service.pause_task(db, OWNER, task.id)
    with pytest.raises(ImmutableTaskFieldError):
        await service.update_task(db, OWNER, task.id, UpdateTask(base_reward_points=1))


@pytest.mark.parametrize("status", [TaskStatus.CLOSED, TaskStatus.ARCHIVED])
async def test_closed_and_archived_not_editable(
    service: TaskService, status: TaskStatus
) -> None:
    task = planted_task(status=status)
    with pytest.raises(ImmutableTaskFieldError) as excinfo:
        await service.update_task(
            db := FakeSession(task),
            OWNER,
            task.id,
            UpdateTask(title="太晚了"),
        )
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR
    assert db.commits == 0


async def test_draft_edit_still_validates_values(service: TaskService) -> None:
    task = planted_task()
    with pytest.raises(BusinessError) as excinfo:
        await service.update_task(
            FakeSession(task), OWNER, task.id, UpdateTask(base_reward_points=0)
        )
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR


# --- ownership ---------------------------------------------------------------


async def test_student_cannot_transition(service: TaskService) -> None:
    task = planted_task()
    with pytest.raises(BusinessError) as excinfo:
        await service.publish_task(FakeSession(task), STUDENT, task.id)
    assert excinfo.value.code == ErrorCode.PERMISSION_DENIED
    assert excinfo.value.status_code == 403
    assert task.status == TaskStatus.DRAFT


async def test_non_owner_teacher_denied_even_for_legal_transition(
    service: TaskService,
) -> None:
    task = planted_task(status=TaskStatus.PUBLISHED)
    # PUBLISHED -> PAUSED is table-legal, so this specifically proves the
    # ownership check fires before the transition check.
    with pytest.raises(BusinessError) as excinfo:
        await service.pause_task(FakeSession(task), STRANGER_TEACHER, task.id)
    assert excinfo.value.code == ErrorCode.PERMISSION_DENIED
    assert task.status == TaskStatus.PUBLISHED


async def test_admin_may_transition_others_tasks(service: TaskService) -> None:
    task = planted_task(status=TaskStatus.PUBLISHED)
    await service.pause_task(FakeSession(task), ADMIN, task.id)
    assert task.status == TaskStatus.PAUSED


async def test_unknown_task_raises_not_found(service: TaskService) -> None:
    with pytest.raises(TaskNotFoundError) as excinfo:
        await service.publish_task(FakeSession(), OWNER, uuid4())
    assert excinfo.value.code == ErrorCode.NOT_FOUND
    assert excinfo.value.status_code == 404


# --- is_claimable -------------------------------------------------------------


def test_is_claimable_by_status(service: TaskService) -> None:
    for status in (
        TaskStatus.DRAFT,
        TaskStatus.PAUSED,
        TaskStatus.CLOSED,
        TaskStatus.ARCHIVED,
    ):
        assert service.is_claimable(planted_task(status=status)) is False
    assert service.is_claimable(planted_task(status=TaskStatus.PUBLISHED)) is True


def test_is_claimable_fixed_deadline_window(service: TaskService) -> None:
    base = {"deadline_mode": "FIXED", "duration_minutes": None}
    at_cutoff = planted_task(
        status=TaskStatus.PUBLISHED,
        fixed_deadline_at=NOW + timedelta(minutes=240),
        **base,
    )
    assert service.is_claimable(at_cutoff, now=NOW) is True  # boundary inclusive
    inside = planted_task(
        status=TaskStatus.PUBLISHED,
        fixed_deadline_at=NOW + timedelta(minutes=239, seconds=59),
        **base,
    )
    assert service.is_claimable(inside, now=NOW) is False
    missing = planted_task(status=TaskStatus.PUBLISHED, fixed_deadline_at=None, **base)
    assert service.is_claimable(missing, now=NOW) is False


def test_is_claimable_uses_injected_clock_by_default(
    service: TaskService, clock: MutableClock
) -> None:
    task = planted_task(
        status=TaskStatus.PUBLISHED,
        deadline_mode="FIXED",
        fixed_deadline_at=NOW + timedelta(minutes=300),
        duration_minutes=None,
    )
    assert service.is_claimable(task) is True
    clock.current = NOW + timedelta(minutes=61)
    assert service.is_claimable(task) is False  # < 240 min remain


# --- DTO shapes ---------------------------------------------------------------


def test_task_public_field_set_is_privacy_safe() -> None:
    names = {f.name for f in fields(TaskPublic)}
    assert names == {
        "id",
        "title",
        "description",
        "task_type",
        "rarity",
        "base_reward_points",
        "status",
        "deadline_mode",
        "fixed_deadline_at",
        "duration_minutes",
        "created_at",
    }
    # owner id, schema internals, flags: unrepresentable by construction
    dto = TaskPublic.from_domain(planted_task())
    assert dto.id is not None
    assert dto.status == TaskStatus.DRAFT
    assert dto.task_type == "DATA_CRAWL"


def test_publish_result_shape() -> None:
    names = {f.name for f in fields(PublishResult)}
    assert names == {"task_id", "status", "published_at", "claimable"}
