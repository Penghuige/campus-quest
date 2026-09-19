# backend/tests/integration/tasks/test_collaborators.py
"""Collaborator grant/removal semantics (spec §4.2; plan 03 T3).

Pinned permission model (the brief's rules, made explicit):

- The task OWNER (and Admin, spec §4.3 全部 Task 管理) may add any Teacher
  with any non-empty subset of the four frozen capabilities.
- An existing collaborator may also add collaborators, but never grant a
  capability outside their own set ("A collaborator cannot grant
  permissions beyond those they possess" — the owner is exempt).
- Anyone else (unrelated Teacher, Student) modifying collaborators is
  PERMISSION_DENIED.
- Removal is owner/Admin only: the spec is silent on collaborator-
  initiated removal (including self-removal), and plan 03 resolves it to
  owner/Admin — documented in collaborator_service.py.
- The target must be a Teacher, verified through the identity
  UserDirectory port (interfaces.md: cross-module reads never touch
  identity ORM) — a Student or an unknown account is rejected.
- Duplicate (task_id, teacher_id) is a typed conflict; the database
  UNIQUE constraint is the race-closing backstop (§31-style, engineering
  §6), so the parity between the service's frozen set and the column
  CHECK is asserted here too.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.directory import SqlAlchemyUserDirectory
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.tasks.collaborator_service import (
    SUPPORTED_COLLABORATOR_PERMISSIONS,
    CollaboratorNotFoundError,
    CollaboratorPermission,
    DuplicateCollaboratorError,
    TaskCollaboratorService,
)
from app.modules.tasks.enums import (
    DeadlineMode,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.models import (
    _COLLABORATOR_CAPABILITIES,
    Task,
    TaskCollaborator,
)
from app.modules.tasks.service import TaskNotFoundError

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)


def _user(*, username: str, role: Role) -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname="测试用户",
        phone_e164=None,
        role=role,
        status=UserStatus.ACTIVE,
    )


def _task(owner: User, **overrides: Any) -> Task:
    fields: dict[str, Any] = {
        "owner_teacher_id": owner.id,
        "title": "小红书考研经验帖数据采集",
        "description": "采集指定关键词下的笔记正文与互动数据。",
        "task_type": TaskType.DATA_CRAWL,
        "rarity": TaskRarity.NORMAL,
        "base_reward_points": 100,
        "status": TaskStatus.PUBLISHED,
        "deadline_mode": DeadlineMode.RELATIVE,
        "duration_minutes": 4320,
        "allowed_file_types": ["CSV"],
        "max_file_size_bytes": 200 * 1024 * 1024,
        "notification_channels": ["SMS"],
    }
    fields.update(overrides)
    return Task(**fields)


def _actor(user: User) -> Actor:
    return Actor(user_id=user.id, role=Role(user.role))


def _service() -> TaskCollaboratorService:
    return TaskCollaboratorService(user_directory=SqlAlchemyUserDirectory())


async def _flush(db_session: AsyncSession, *objects: Any) -> None:
    db_session.add_all(objects)
    await db_session.flush()


async def _seed_task(db_session: AsyncSession, owner: User) -> Task:
    task = _task(owner)
    await _flush(db_session, task)
    return task


@pytest.mark.integration
async def test_capability_set_matches_database_check() -> None:
    """The service's frozen capability set is exactly the closed set the
    task_collaborators.permissions CHECK guards (<@ ARRAY[...]) — drift
    would let the service grant a value PostgreSQL rejects."""
    assert frozenset(_COLLABORATOR_CAPABILITIES) == SUPPORTED_COLLABORATOR_PERMISSIONS
    assert {member.value for member in CollaboratorPermission} == set(
        _COLLABORATOR_CAPABILITIES
    )


@pytest.mark.integration
async def test_owner_adds_collaborator_with_normalized_permissions(
    db_session: AsyncSession,
) -> None:
    """Owner adds a Teacher; raw strings are upper-cased, stripped,
    de-duplicated, and stored in canonical capability order."""
    owner = _user(username="teacher0001", role=Role.TEACHER)
    colleague = _user(username="teacher0002", role=Role.TEACHER)
    await _flush(db_session, owner, colleague)
    task = await _seed_task(db_session, owner)

    added = await _service().add_collaborator(
        db_session,
        _actor(owner),
        task.id,
        colleague.id,
        [" view_task ", "VIEW_TASK", "review_submissions"],
    )

    assert added.permissions == ["VIEW_TASK", "REVIEW_SUBMISSIONS"]
    persisted = await db_session.scalar(
        select(TaskCollaborator).where(
            TaskCollaborator.task_id == task.id,
            TaskCollaborator.teacher_id == colleague.id,
        )
    )
    assert persisted is not None
    assert persisted.permissions == ["VIEW_TASK", "REVIEW_SUBMISSIONS"]


@pytest.mark.integration
async def test_empty_or_unknown_permissions_rejected(
    db_session: AsyncSession,
) -> None:
    """The granted set must be a non-empty subset of the four frozen
    capabilities; anything else fails with VALIDATION_ERROR before the
    database CHECK is reached."""
    owner = _user(username="teacher0001", role=Role.TEACHER)
    colleague = _user(username="teacher0002", role=Role.TEACHER)
    await _flush(db_session, owner, colleague)
    task = await _seed_task(db_session, owner)
    service = _service()

    with pytest.raises(BusinessError) as empty_error:
        await service.add_collaborator(
            db_session, _actor(owner), task.id, colleague.id, []
        )
    assert empty_error.value.code == ErrorCode.VALIDATION_ERROR
    assert empty_error.value.status_code == 400

    with pytest.raises(BusinessError) as unknown_error:
        await service.add_collaborator(
            db_session, _actor(owner), task.id, colleague.id, ["VIEW_TASK", "ROOT"]
        )
    assert unknown_error.value.code == ErrorCode.VALIDATION_ERROR
    assert unknown_error.value.details == {"unsupported": ["ROOT"]}

    assert await db_session.scalar(select(TaskCollaborator)) is None


@pytest.mark.integration
async def test_owner_grants_any_capability(db_session: AsyncSession) -> None:
    """The owner (unlike a collaborator) may grant the full frozen set —
    including capabilities no explicit row grants them."""
    owner = _user(username="teacher0001", role=Role.TEACHER)
    colleague = _user(username="teacher0002", role=Role.TEACHER)
    await _flush(db_session, owner, colleague)
    task = await _seed_task(db_session, owner)

    added = await _service().add_collaborator(
        db_session,
        _actor(owner),
        task.id,
        colleague.id,
        list(CollaboratorPermission),
    )

    assert set(added.permissions) == set(SUPPORTED_COLLABORATOR_PERMISSIONS)


@pytest.mark.integration
async def test_collaborator_cannot_grant_beyond_own_permissions(
    db_session: AsyncSession,
) -> None:
    """A collaborator holding MANAGE_ASSIGNMENTS but not
    REVIEW_SUBMISSIONS cannot grant REVIEW_SUBMISSIONS; nothing is
    persisted for the blocked target."""
    owner = _user(username="teacher0001", role=Role.TEACHER)
    limited = _user(username="teacher0002", role=Role.TEACHER)
    target = _user(username="teacher0003", role=Role.TEACHER)
    await _flush(db_session, owner, limited, target)
    task = await _seed_task(db_session, owner)
    service = _service()
    await service.add_collaborator(
        db_session,
        _actor(owner),
        task.id,
        limited.id,
        [CollaboratorPermission.MANAGE_ASSIGNMENTS],
    )

    with pytest.raises(BusinessError) as denied:
        await service.add_collaborator(
            db_session,
            _actor(limited),
            task.id,
            target.id,
            [
                CollaboratorPermission.MANAGE_ASSIGNMENTS,
                CollaboratorPermission.REVIEW_SUBMISSIONS,
            ],
        )
    assert denied.value.code == ErrorCode.PERMISSION_DENIED
    assert denied.value.status_code == 403

    assert (
        await db_session.scalar(
            select(TaskCollaborator).where(TaskCollaborator.teacher_id == target.id)
        )
        is None
    )


@pytest.mark.integration
async def test_collaborator_grants_within_own_permissions(
    db_session: AsyncSession,
) -> None:
    """The flip side of the grant-beyond rule: a collaborator granting a
    subset of their own set succeeds (the brief's rule presupposes this)."""
    owner = _user(username="teacher0001", role=Role.TEACHER)
    grantor = _user(username="teacher0002", role=Role.TEACHER)
    target = _user(username="teacher0003", role=Role.TEACHER)
    await _flush(db_session, owner, grantor, target)
    task = await _seed_task(db_session, owner)
    service = _service()
    await service.add_collaborator(
        db_session,
        _actor(owner),
        task.id,
        grantor.id,
        [CollaboratorPermission.VIEW_TASK, CollaboratorPermission.MANAGE_ASSIGNMENTS],
    )

    added = await service.add_collaborator(
        db_session,
        _actor(grantor),
        task.id,
        target.id,
        [CollaboratorPermission.MANAGE_ASSIGNMENTS],
    )

    assert added.permissions == ["MANAGE_ASSIGNMENTS"]


@pytest.mark.integration
async def test_unrelated_teacher_and_student_denied(
    db_session: AsyncSession,
) -> None:
    """A Teacher with no standing on the task (and any Student) can
    neither add nor remove collaborators."""
    owner = _user(username="teacher0001", role=Role.TEACHER)
    colleague = _user(username="teacher0002", role=Role.TEACHER)
    outsider = _user(username="teacher0003", role=Role.TEACHER)
    student = _user(username="20250010001", role=Role.STUDENT)
    await _flush(db_session, owner, colleague, outsider, student)
    task = await _seed_task(db_session, owner)
    service = _service()
    await service.add_collaborator(
        db_session,
        _actor(owner),
        task.id,
        colleague.id,
        [CollaboratorPermission.VIEW_TASK],
    )

    for actor in (_actor(outsider), _actor(student)):
        with pytest.raises(BusinessError) as add_denied:
            await service.add_collaborator(
                db_session,
                actor,
                task.id,
                colleague.id,
                [CollaboratorPermission.VIEW_TASK],
            )
        assert add_denied.value.code == ErrorCode.PERMISSION_DENIED

        with pytest.raises(BusinessError) as remove_denied:
            await service.remove_collaborator(db_session, actor, task.id, colleague.id)
        assert remove_denied.value.code == ErrorCode.PERMISSION_DENIED

    # The blocked attempts changed nothing.
    collaborators = (await db_session.scalars(select(TaskCollaborator))).all()
    assert len(collaborators) == 1


@pytest.mark.integration
async def test_non_teacher_target_rejected_via_directory(
    db_session: AsyncSession,
) -> None:
    """The target's role is resolved through the UserDirectory port (never
    identity ORM): a Student and an unknown account are both rejected with
    VALIDATION_ERROR."""
    owner = _user(username="teacher0001", role=Role.TEACHER)
    admin = _user(username="admin0001", role=Role.ADMIN)
    student = _user(username="20250010001", role=Role.STUDENT)
    await _flush(db_session, owner, admin, student)
    task = await _seed_task(db_session, owner)
    service = _service()

    for target_id in (student.id, admin.id, uuid4()):
        with pytest.raises(BusinessError) as rejected:
            await service.add_collaborator(
                db_session, _actor(owner), task.id, target_id, ["VIEW_TASK"]
            )
        assert rejected.value.code == ErrorCode.VALIDATION_ERROR
        assert rejected.value.status_code == 400

    assert await db_session.scalar(select(TaskCollaborator)) is None


@pytest.mark.integration
async def test_duplicate_collaborator_conflict(db_session: AsyncSession) -> None:
    """Adding the same (task_id, teacher_id) twice raises the typed
    conflict; the UNIQUE constraint is the race backstop."""
    owner = _user(username="teacher0001", role=Role.TEACHER)
    colleague = _user(username="teacher0002", role=Role.TEACHER)
    await _flush(db_session, owner, colleague)
    task = await _seed_task(db_session, owner)
    service = _service()
    await service.add_collaborator(
        db_session, _actor(owner), task.id, colleague.id, ["VIEW_TASK"]
    )

    with pytest.raises(DuplicateCollaboratorError) as conflict:
        await service.add_collaborator(
            db_session, _actor(owner), task.id, colleague.id, ["REVIEW_SUBMISSIONS"]
        )
    assert conflict.value.code == ErrorCode.VALIDATION_ERROR
    assert conflict.value.status_code == 409


@pytest.mark.integration
async def test_owner_and_admin_remove_collaborator(
    db_session: AsyncSession,
) -> None:
    """Removal is owner/Admin only; both paths delete the row."""
    owner = _user(username="teacher0001", role=Role.TEACHER)
    admin = _user(username="admin0001", role=Role.ADMIN)
    colleague = _user(username="teacher0002", role=Role.TEACHER)
    await _flush(db_session, owner, admin, colleague)
    task = await _seed_task(db_session, owner)
    service = _service()
    await service.add_collaborator(
        db_session, _actor(owner), task.id, colleague.id, ["VIEW_TASK"]
    )

    await service.remove_collaborator(db_session, _actor(admin), task.id, colleague.id)

    assert await db_session.scalar(select(TaskCollaborator)) is None

    await service.add_collaborator(
        db_session, _actor(owner), task.id, colleague.id, ["VIEW_TASK"]
    )
    await service.remove_collaborator(db_session, _actor(owner), task.id, colleague.id)

    assert await db_session.scalar(select(TaskCollaborator)) is None


@pytest.mark.integration
async def test_collaborator_cannot_remove_not_even_self(
    db_session: AsyncSession,
) -> None:
    """Spec §4.2 grants collaborator *management* to the owner only; the
    spec is silent on self-removal, and plan 03 resolves it to owner/Admin
    — a collaborator removing anyone (themselves included) is denied."""
    owner = _user(username="teacher0001", role=Role.TEACHER)
    colleague = _user(username="teacher0002", role=Role.TEACHER)
    other = _user(username="teacher0003", role=Role.TEACHER)
    await _flush(db_session, owner, colleague, other)
    task = await _seed_task(db_session, owner)
    service = _service()
    await service.add_collaborator(
        db_session, _actor(owner), task.id, colleague.id, ["VIEW_TASK"]
    )
    await service.add_collaborator(
        db_session, _actor(owner), task.id, other.id, ["MODERATE_COMMUNITY"]
    )

    with pytest.raises(BusinessError) as self_denied:
        await service.remove_collaborator(
            db_session, _actor(colleague), task.id, colleague.id
        )
    assert self_denied.value.code == ErrorCode.PERMISSION_DENIED

    with pytest.raises(BusinessError) as other_denied:
        await service.remove_collaborator(
            db_session, _actor(colleague), task.id, other.id
        )
    assert other_denied.value.code == ErrorCode.PERMISSION_DENIED

    assert len((await db_session.scalars(select(TaskCollaborator))).all()) == 2


@pytest.mark.integration
async def test_remove_missing_collaborator_not_found(
    db_session: AsyncSession,
) -> None:
    """Removing an absent collaborator is the typed NOT_FOUND, mirroring
    TaskNotFoundError's aggregate semantics."""
    owner = _user(username="teacher0001", role=Role.TEACHER)
    stranger = _user(username="teacher0002", role=Role.TEACHER)
    await _flush(db_session, owner, stranger)
    task = await _seed_task(db_session, owner)

    with pytest.raises(CollaboratorNotFoundError) as missing:
        await _service().remove_collaborator(
            db_session, _actor(owner), task.id, stranger.id
        )
    assert missing.value.code == ErrorCode.NOT_FOUND
    assert missing.value.status_code == 404


@pytest.mark.integration
async def test_unknown_task_not_found(db_session: AsyncSession) -> None:
    """Add/remove against a missing task raise the shared typed
    TaskNotFoundError before any authorization leak."""
    owner = _user(username="teacher0001", role=Role.TEACHER)
    colleague = _user(username="teacher0002", role=Role.TEACHER)
    await _flush(db_session, owner, colleague)
    service = _service()

    with pytest.raises(TaskNotFoundError):
        await service.add_collaborator(
            db_session, _actor(owner), uuid4(), colleague.id, ["VIEW_TASK"]
        )
    with pytest.raises(TaskNotFoundError):
        await service.remove_collaborator(
            db_session, _actor(owner), uuid4(), colleague.id
        )
