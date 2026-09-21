# backend/tests/workers/test_requeue_stale_validating.py
"""Stale-VALIDATING recovery scan tests (PR #2 hardening, final-review
sub-F2; spec §10 step 8 / §32).

The discovery predicate runs against real PostgreSQL: a submission
stuck in VALIDATING whose NEWEST run row started before the threshold
qualifies; a fresh in-flight run (newest run row recent) does not; a
terminal submission never does even when it carries an old interrupted
run row in its history (the terminal gate is the projection, not the
history). The wiring itself — task registration, autoretry, the beat
entry — is pinned by test_celery_wiring.py.

Harness notes (the test_expire_claims conventions): explicit committed
sessions from a NullPool engine; real commits require real cleanup in
FK order (validations -> submissions -> claims -> assignments -> tasks
-> users); usernames embed a per-run token.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.submissions.models import Submission, SubmissionValidation
from app.modules.tasks.enums import (
    AssignmentAvailability,
    ClaimStatus,
    DeadlineMode,
    RewardLockStatus,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.models import Assignment, AssignmentClaim, Task
from app.workers.jobs.requeue_stale_validating import collect_stale_validating_ids

pytestmark = pytest.mark.integration

_TEST_DATABASE_MARKER = "campusquest_test"
_DEFAULT_DATABASE_URL = (
    "postgresql+asyncpg://test:test@localhost:15432/campusquest_test"
)

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)

_NOW = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


def _database_url() -> str:
    """The integration database URL, refusing non-test databases."""
    from sqlalchemy.engine import make_url

    url = os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)
    database = make_url(url).database or ""
    if _TEST_DATABASE_MARKER not in database:
        pytest.fail(
            f"Refusing requeue-scan tests against non-test database "
            f"{database!r} (DATABASE_URL={url!r}): the database name must "
            f"contain {_TEST_DATABASE_MARKER!r}."
        )
    return url


def specs_names() -> tuple[str, ...]:
    return ("stale", "fresh", "terminal_history")


def _factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed_world(
    maker: async_sessionmaker[AsyncSession], run: str
) -> dict[str, UUID]:
    """Three submissions over three claims, one task:

    - ``stale``: VALIDATING, its only run row started 2h ago (threshold
      30 min) — the wedged shape, MUST qualify;
    - ``fresh``: VALIDATING, a fresh run row started 5 min ago (an
      in-flight retry ladder) — MUST NOT qualify;
    - ``terminal_history``: VALIDATION_FAILED projection carrying an
      interrupted VALIDATING run row from 2h ago in its history — MUST
      NOT qualify (the projection is the gate, the history is honest
      history).
    """
    async with maker() as session:
        teacher = User(
            username=f"t{run}",
            password_hash=_PASSWORD_HASH,
            nickname=f"老师{run[-4:]}",
            phone_e164=None,
            role=Role.TEACHER,
            status=UserStatus.ACTIVE,
        )
        session.add(teacher)
        await session.flush()
        # One student per scenario: the ACTIVE-claim partial unique
        # index (user_id, task_id) forbids two concurrent claims of one
        # task by the same student.
        students: dict[str, User] = {}
        for index, name in enumerate(specs_names()):
            student = User(
                username=f"2025{run}00{index + 1}",
                password_hash=_PASSWORD_HASH,
                nickname=f"同学{run[-4:]}",
                phone_e164=None,
                role=Role.STUDENT,
                status=UserStatus.ACTIVE,
            )
            session.add(student)
            students[name] = student
        await session.flush()
        task = Task(
            owner_teacher_id=teacher.id,
            title="校园食堂满意度问卷采集",
            description="采集问卷数据。",
            task_type=TaskType.DATA_CRAWL,
            rarity=TaskRarity.NORMAL,
            base_reward_points=100,
            status=TaskStatus.PUBLISHED,
            deadline_mode=DeadlineMode.RELATIVE,
            duration_minutes=4320,
            submission_schema={"columns": [{"name": "note", "type": "string"}]},
            submission_schema_version=1,
            allowed_file_types=["CSV"],
            max_file_size_bytes=10 * 1024 * 1024,
            notification_channels=["SMS"],
        )
        session.add(task)
        await session.flush()
        ids: dict[str, UUID] = {}
        specs = {
            "stale": ("VALIDATING", _NOW - timedelta(hours=2)),
            "fresh": ("VALIDATING", _NOW - timedelta(minutes=5)),
            "terminal_history": ("VALIDATION_FAILED", _NOW - timedelta(hours=2)),
        }
        for name, (validation_status, run_started) in specs.items():
            assignment = Assignment(
                task_id=task.id,
                platform="xiaohongshu",
                keyword=f"食堂{name}",
                availability_status=AssignmentAvailability.OCCUPIED,
            )
            session.add(assignment)
            await session.flush()
            claim = AssignmentClaim(
                assignment_id=assignment.id,
                task_id=task.id,
                user_id=students[name].id,
                status=ClaimStatus.VALIDATING.value,
                claimed_at=_NOW - timedelta(days=3),
                deadline_at=_NOW - timedelta(days=1),
                grace_deadline_at=_NOW - timedelta(hours=20),
                reward_policy_snapshot={"version": 1},
                base_reward_points_snapshot=100,
                submission_schema_version=1,
                reward_lock_status=RewardLockStatus.NONE,
            )
            session.add(claim)
            await session.flush()
            submission = Submission(
                claim_id=claim.id,
                version=1,
                object_key=f"submissions/{claim.id}/{uuid4()}",
                original_filename="数据.csv",
                declared_type="CSV",
                file_size=1024,
                submitted_at=_NOW - timedelta(days=1),
                validation_status=validation_status,
                retention_until=_NOW + timedelta(days=180),
            )
            session.add(submission)
            await session.flush()
            claim.latest_submission_id = submission.id
            session.add(
                SubmissionValidation(
                    submission_id=submission.id,
                    parser_version="pending",
                    status="VALIDATING",
                    started_at=run_started,
                )
            )
            ids[name] = submission.id
        await session.commit()
        ids["task"] = task.id
        ids["teacher"] = teacher.id
        for student in students.values():
            ids[f"student:{student.username}"] = student.id
        return ids


async def _cleanup_world(
    maker: async_sessionmaker[AsyncSession], ids: dict[str, UUID]
) -> None:
    async with maker() as session:
        await session.execute(
            delete(SubmissionValidation).where(
                SubmissionValidation.submission_id.in_(
                    [
                        ids["stale"],
                        ids["fresh"],
                        ids["terminal_history"],
                    ]
                )
            )
        )
        await session.execute(
            delete(Submission).where(
                Submission.id.in_([ids["stale"], ids["fresh"], ids["terminal_history"]])
            )
        )
        await session.execute(
            delete(AssignmentClaim).where(AssignmentClaim.task_id == ids["task"])
        )
        await session.execute(
            delete(Assignment).where(Assignment.task_id == ids["task"])
        )
        await session.execute(delete(Task).where(Task.id == ids["task"]))
        user_ids = [
            value
            for key, value in ids.items()
            if key == "teacher" or key.startswith("student:")
        ]
        await session.execute(delete(User).where(User.id.in_(user_ids)))
        await session.commit()


def test_stale_validating_discovery_judges_the_newest_run_row() -> None:
    """Only the genuinely wedged row qualifies: the stale VALIDATING
    submission whose newest run row predates the threshold. A fresh
    in-flight run never qualifies, and neither does a terminal
    submission merely carrying interrupted-run history."""
    maker = _factory()
    run = uuid4().hex[:8]
    ids = asyncio.run(_seed_world(maker, run))
    try:

        async def _discover() -> list[UUID]:
            async with maker() as session:
                return await collect_stale_validating_ids(
                    session,
                    _NOW,
                    stale_after=timedelta(minutes=30),
                    limit=500,
                )

        discovered = asyncio.run(_discover())
        assert ids["stale"] in discovered
        assert ids["fresh"] not in discovered
        assert ids["terminal_history"] not in discovered

        # The batch ceiling bounds the scan (the shared scan shape).
        async def _limited() -> list[UUID]:
            async with maker() as session:
                return await collect_stale_validating_ids(
                    session,
                    _NOW,
                    stale_after=timedelta(minutes=30),
                    limit=0,
                )

        assert asyncio.run(_limited()) == []
    finally:
        asyncio.run(_cleanup_world(maker, ids))
