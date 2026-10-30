"""Build the load-test world: hot task, pre-seeded claims, offline tokens.

Spec §Scenarios/§Auth: the S1 deadline-rush scenario needs a PUBLISHED
FIXED-deadline task whose claims already exist (§9.1 stops new claims
inside the cutoff window) and authenticated users whose tokens never
touched the login route (the per-IP login limiter would swallow a
single-generator rush). This module is the single source of that world;
``scripts/seed_load_world.py`` is its CLI shell.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.security import AccessTokenCodec, hash_password
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.identity.session_service import SessionService
from app.modules.submissions.models import (
    RewardLockHistory,
    Submission,
    SubmissionReview,
    SubmissionValidation,
    UploadIntent,
)
from app.modules.tasks.claim_service import REWARD_POLICY_SNAPSHOT_V1
from app.modules.tasks.commands import CreateTask
from app.modules.tasks.deadlines import compute_claim_deadlines
from app.modules.tasks.enums import (
    AssignmentAvailability,
    ClaimStatus,
    DeadlineMode,
    RewardLockStatus,
)
from app.modules.tasks.models import Assignment, AssignmentClaim, Task
from app.modules.tasks.service import TaskService
from loadtest.env import loadtest_env

LOAD_TASK_TITLE = "loadtest-capacity-hot-task"
LOAD_TEACHER_EMAIL = "loadtest-teacher@campus.example.edu"
LOAD_PASSWORD = "loadtest-world-2026"


class LoadWorldExistsError(RuntimeError):
    """A load world already exists; pass force=True to replace it."""


async def seed_world(
    *,
    claim_slots: int,
    race_users: int,
    race_slots: int,
    deadline_in_minutes: int,
    out_dir: Path,
    students: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Create (or replace) the load world and mint one token per student.

    Returns the world dict and writes it to ``out_dir/world.json``. The
    first ``claim_slots`` users carry pre-seeded claims (S1 submitters);
    the next ``race_users`` have ``claim_id: null`` (S2 racers).
    """

    if students is None:
        students = claim_slots + race_users
    if students < claim_slots + race_users:
        raise ValueError(
            f"students={students} < claim_slots + race_users "
            f"({claim_slots} + {race_users})"
        )

    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    clock = SystemClock()
    now = clock.now()
    try:
        async with maker() as db:
            existing = await db.scalar(
                select(Task).where(Task.title == LOAD_TASK_TITLE)
            )
            if existing is not None:
                if not force:
                    raise LoadWorldExistsError(
                        f"a load world already exists (task {existing.id}); "
                        "pass --force to replace it, or reset the test DB"
                    )
                await _drop_world(db, existing)

            teacher = await _ensure_teacher(db, now)
            actor = Actor(user_id=teacher.id, role=Role(teacher.role))
            service = TaskService(
                clock=clock,
                max_upload_bytes=settings.max_upload_bytes_default,
            )
            # claim_cutoff_minutes=0 (found live in the S2 smoke: §9.1's
            # default 240-minute cutoff rejects every claim on a task
            # whose deadline is minutes away, so 30-for-10 gave 1 winner).
            # Zero cutoff lets S2 race the SAME tight-deadline task S1
            # submits against, and publish validation accepts the tight
            # deadline directly — no post-publish surgery. The §9.1
            # cutoff behavior itself is the product claim suite's turf.
            run_deadline = now + timedelta(minutes=deadline_in_minutes)
            task = await service.create_task(
                db,
                actor,
                CreateTask(
                    title=LOAD_TASK_TITLE,
                    description=(
                        "Capacity-suite hot task (seed tooling; not product data)."
                    ),
                    base_reward_points=100,
                    deadline_mode=DeadlineMode.FIXED,
                    fixed_deadline_at=run_deadline,
                    # Structured family only: the product forbids mixing
                    # structured (CSV/XLSX/SQLITE) and document
                    # (DOCX/PDF) types on one task (publish validation).
                    # Structured also mirrors production reality — the
                    # recruitment data-collection tasks are CSV/XLSX.
                    allowed_file_types=(
                        "CSV",
                        "XLSX",
                        "SQLITE",
                    ),
                    max_file_size_bytes=settings.max_upload_bytes_default,
                    claim_cutoff_minutes=0,
                    submission_schema_version=1,
                    target_count=claim_slots + race_slots,
                ),
            )
            try:
                await service.publish_task(db, actor, task.id)
            except Exception:
                # A DRAFT that cannot publish must not linger as an
                # orphan blocking the next seed run (create_task commits).
                await _drop_world(db, task)
                await db.commit()
                raise

            users = await _ensure_students(db, students, now)
            assignments = (
                await db.scalars(
                    select(Assignment)
                    .where(Assignment.task_id == task.id)
                    .order_by(Assignment.keyword)
                )
            ).all()
            deadlines = compute_claim_deadlines(task, now)
            claims: list[AssignmentClaim] = []
            for user, assignment in zip(
                users[:claim_slots],
                assignments[:claim_slots],
                strict=True,
            ):
                claim = AssignmentClaim(
                    assignment_id=assignment.id,
                    task_id=task.id,
                    user_id=user.id,
                    status=ClaimStatus.CLAIMED,
                    claimed_at=now,
                    deadline_at=deadlines.deadline_at,
                    grace_deadline_at=deadlines.grace_deadline_at,
                    reward_policy_snapshot=dict(REWARD_POLICY_SNAPSHOT_V1),
                    base_reward_points_snapshot=task.base_reward_points,
                    submission_schema_version=task.submission_schema_version,
                    reward_lock_status=RewardLockStatus.NONE,
                )
                assignment.availability_status = AssignmentAvailability.OCCUPIED
                db.add(claim)
                claims.append(claim)
            await db.flush()

            # Offline token minting: the SAME issuance path the login
            # route calls (issue_session), never HTTP — the per-IP login
            # limiter cannot see it by construction (spec §Auth).
            session_service = SessionService(
                clock=clock,
                access_codec=AccessTokenCodec(
                    secret=settings.token_secret,
                    ttl_minutes=settings.access_token_ttl_minutes,
                ),
                refresh_token_ttl_days=settings.refresh_token_ttl_days,
            )
            entries: list[dict[str, Any]] = []
            for user in users:
                _, tokens = await session_service.issue_session(db, user=user, now=now)
                entries.append(
                    {
                        "user_id": str(user.id),
                        "username": user.username,
                        "access_token": tokens.access_token,
                        "refresh_token": tokens.refresh_token,
                        "claim_id": None,
                    }
                )
            for entry, claim in zip(entries[:claim_slots], claims, strict=True):
                entry["claim_id"] = str(claim.id)
            await db.commit()

            world: dict[str, Any] = {
                "api_base": loadtest_env().api_base,
                "task_id": str(task.id),
                "deadline_at": run_deadline.isoformat(),
                "slots_total": claim_slots + race_slots,
                "users": entries,
            }
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "world.json").write_text(json.dumps(world, indent=2))
            return world
    finally:
        await engine.dispose()


async def _ensure_teacher(db: AsyncSession, now) -> User:
    teacher = await db.scalar(
        select(User).where(User.email_normalized == LOAD_TEACHER_EMAIL)
    )
    if teacher is not None:
        return teacher
    teacher = User(
        username=LOAD_TEACHER_EMAIL,
        password_hash=hash_password(LOAD_PASSWORD),
        nickname="压测教师",
        phone_e164=None,
        email_normalized=LOAD_TEACHER_EMAIL,
        email_verified_at=now,
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
    )
    db.add(teacher)
    await db.flush()
    return teacher


async def _ensure_students(db: AsyncSession, count: int, now: datetime) -> list[User]:
    """Numeric usernames 31000001.. (login form allows 6-20 digits)."""

    usernames = [f"31{index:06d}" for index in range(1, count + 1)]
    existing = {
        user.username: user
        for user in await db.scalars(select(User).where(User.username.in_(usernames)))
    }
    users: list[User] = []
    for username in usernames:
        user = existing.get(username)
        if user is None:
            user = User(
                username=username,
                password_hash=hash_password(LOAD_PASSWORD),
                nickname=f"压测同学{username[-4:]}",
                phone_e164=None,
                role=Role.STUDENT,
                status=UserStatus.ACTIVE,
            )
            db.add(user)
            await db.flush()
        users.append(user)
    return users


async def _drop_world(db: AsyncSession, task: Task) -> None:
    """Remove the previous load world's rows, submission chain first.

    FK order matters (found the hard way by Task 5's dry-run): intents,
    validations, reviews, and reward-lock history reference submissions,
    and submissions reference claims. Audit history is plain UUIDs with
    no FK — it survives task deletion, matching product history
    semantics for a tooling-only world. Uploaded objects stay in the
    bucket; capacity worlds are throwaway.
    """

    claim_ids = select(AssignmentClaim.id).where(AssignmentClaim.task_id == task.id)
    submission_ids = select(Submission.id).where(Submission.claim_id.in_(claim_ids))
    await db.execute(
        delete(RewardLockHistory).where(
            RewardLockHistory.submission_id.in_(submission_ids)
        )
    )
    await db.execute(
        delete(SubmissionReview).where(
            SubmissionReview.submission_id.in_(submission_ids)
        )
    )
    await db.execute(
        delete(SubmissionValidation).where(
            SubmissionValidation.submission_id.in_(submission_ids)
        )
    )
    await db.execute(delete(UploadIntent).where(UploadIntent.claim_id.in_(claim_ids)))
    await db.execute(delete(Submission).where(Submission.claim_id.in_(claim_ids)))
    await db.execute(delete(AssignmentClaim).where(AssignmentClaim.task_id == task.id))
    await db.execute(delete(Assignment).where(Assignment.task_id == task.id))
    await db.execute(delete(Task).where(Task.id == task.id))
    await db.flush()
