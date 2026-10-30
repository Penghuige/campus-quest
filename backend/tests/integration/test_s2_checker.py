"""Integration: the S2 oversell/fairness checker (plan Task 7).

S2's guarantees are the two partial unique indexes — one active claim per
Assignment, one active claim per (user, task). The checker verifies the
observed data honors them; the tests verify the indexes themselves fire
on independent-session inserts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.modules.identity.models import User, UserSession
from app.modules.submissions.models import Submission
from app.modules.tasks.enums import ClaimStatus
from app.modules.tasks.models import Assignment, AssignmentClaim, Task
from loadtest.check_s2 import run_checks
from loadtest.seed import LOAD_TASK_TITLE, seed_world

pytestmark = pytest.mark.integration


def _summary(*, ok: int, rejected: int, err: int = 0) -> dict[str, Any]:
    return {
        "scenario": "s2",
        "by_name": {"s2:claim": {"ok": ok, "rejected": rejected, "err": err}},
    }


async def _engine_maker() -> tuple[Any, Any]:
    engine = create_async_engine(get_settings().database_url)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _claim(db: Any, claim_id: UUID) -> AssignmentClaim | None:
    return await db.get(AssignmentClaim, claim_id)


async def _first_open_assignment(db: Any, task_id: UUID) -> Assignment:
    return (
        await db.scalars(
            select(Assignment)
            .where(
                Assignment.task_id == task_id,
                Assignment.availability_status == "AVAILABLE",
            )
            .limit(1)
        )
    ).first()


async def _plant_active_claim(
    db: Any, task: Task, assignment: Assignment, user_id: UUID
) -> AssignmentClaim:
    now = datetime.now(UTC)
    claim = AssignmentClaim(
        assignment_id=assignment.id,
        task_id=task.id,
        user_id=user_id,
        status=ClaimStatus.CLAIMED,
        claimed_at=now,
        deadline_at=task.fixed_deadline_at,
        grace_deadline_at=task.fixed_deadline_at + timedelta(hours=24),
        reward_policy_snapshot={},
        base_reward_points_snapshot=task.base_reward_points,
        submission_schema_version=task.submission_schema_version,
        reward_lock_status="NONE",
    )
    db.add(claim)
    assignment.availability_status = "OCCUPIED"
    await db.flush()
    return claim


async def _load_task(db: Any) -> Task:
    return (await db.scalars(select(Task).where(Task.title == LOAD_TASK_TITLE))).one()


async def test_checker_green_when_racers_fill_slots_exactly(
    tmp_path: Path,
) -> None:
    world = await seed_world(
        students=4,
        claim_slots=1,
        race_users=3,
        race_slots=2,
        deadline_in_minutes=25,
        out_dir=tmp_path,
        force=True,
    )
    engine, maker = await _engine_maker()
    try:
        async with maker() as db:
            task = await _load_task(db)
            racers = [u for u in world["users"] if u["claim_id"] is None]
            winner_ids = racers[:2]
            for entry in winner_ids:
                assignment = await _first_open_assignment(db, task.id)
                await _plant_active_claim(db, task, assignment, UUID(entry["user_id"]))
            await db.commit()
            report = await run_checks(
                db,
                world,
                _summary(ok=2, rejected=len(racers) - 2),
            )
        assert report["ok"] is True, report["violations"]
        assert report["metrics"]["winners"] == 2
    finally:
        await _cleanup(world)


async def test_checker_flags_oversell_mismatch(tmp_path: Path) -> None:
    world = await seed_world(
        students=4,
        claim_slots=1,
        race_users=3,
        race_slots=2,
        deadline_in_minutes=25,
        out_dir=tmp_path,
        force=True,
    )
    engine, maker = await _engine_maker()
    try:
        async with maker() as db:
            task = await _load_task(db)
            racers = [u for u in world["users"] if u["claim_id"] is None]
            for entry in racers[:1]:  # only ONE claim actually landed
                assignment = await _first_open_assignment(db, task.id)
                await _plant_active_claim(db, task, assignment, UUID(entry["user_id"]))
            await db.commit()
            # summary lies: claims 2 winners, 1 rejected
            report = await run_checks(db, world, _summary(ok=2, rejected=1))
        assert report["ok"] is False
        assert any("winners" in v for v in report["violations"])
    finally:
        await _cleanup(world)


async def test_database_rejects_double_active_claim_per_assignment(
    tmp_path: Path,
) -> None:
    world = await seed_world(
        students=4,
        claim_slots=1,
        race_users=3,
        race_slots=2,
        deadline_in_minutes=25,
        out_dir=tmp_path,
        force=True,
    )
    engine, maker = await _engine_maker()
    try:
        async with maker() as db:
            task = await _load_task(db)
            racers = [u for u in world["users"] if u["claim_id"] is None]
            assignment = await _first_open_assignment(db, task.id)
            await _plant_active_claim(db, task, assignment, UUID(racers[0]["user_id"]))
            await db.commit()

        async with maker() as db2:  # independent session (repo pattern)
            now = datetime.now(UTC)
            db2.add(
                AssignmentClaim(
                    assignment_id=assignment.id,
                    task_id=task.id,
                    user_id=UUID(racers[1]["user_id"]),
                    status=ClaimStatus.CLAIMED,
                    claimed_at=now,
                    deadline_at=task.fixed_deadline_at,
                    grace_deadline_at=task.fixed_deadline_at + timedelta(hours=24),
                    reward_policy_snapshot={},
                    base_reward_points_snapshot=task.base_reward_points,
                    submission_schema_version=(task.submission_schema_version),
                    reward_lock_status="NONE",
                )
            )
            with pytest.raises(IntegrityError):
                await db2.commit()
            await db2.rollback()
    finally:
        await _cleanup(world)


async def _cleanup(world: dict[str, Any]) -> None:
    engine, maker = await _engine_maker()
    async with maker() as db:
        task = await db.scalar(select(Task).where(Task.title == LOAD_TASK_TITLE))
        if task is not None:
            claim_ids = select(AssignmentClaim.id).where(
                AssignmentClaim.task_id == task.id
            )
            await db.execute(
                delete(Submission).where(Submission.claim_id.in_(claim_ids))
            )
            await db.execute(
                delete(AssignmentClaim).where(AssignmentClaim.task_id == task.id)
            )
            await db.execute(delete(Assignment).where(Assignment.task_id == task.id))
            await db.delete(task)
        for entry in world["users"]:
            user = await db.scalar(
                select(User).where(User.username == entry["username"])
            )
            if user is not None:
                await db.execute(
                    delete(UserSession).where(UserSession.user_id == user.id)
                )
                await db.delete(user)
        await db.commit()
    await engine.dispose()
