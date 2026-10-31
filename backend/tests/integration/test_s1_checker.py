"""Integration: the S1 correctness checker (plan Task 6).

The checker is the capacity report's centerpiece: it proves, from the
database, that the repo's guarantees held under load — no lost or
duplicate submissions, deadline fairness per spec §9.3, full validation
drain, and tier oracles on every reward-lock row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.modules.identity.models import User, UserSession
from app.modules.submissions.models import (
    RewardLockHistory,
    Submission,
    SubmissionValidation,
)
from app.modules.tasks.models import Assignment, AssignmentClaim, Task
from loadtest.check_s1 import run_checks
from loadtest.seed import LOAD_TASK_TITLE, seed_world

pytestmark = pytest.mark.integration


def _summary(complete_ok: int) -> dict[str, Any]:
    return {
        "scenario": "s1",
        "by_name": {"s1:complete": {"ok": complete_ok}},
    }


async def _engine_maker() -> tuple[Any, Any]:
    engine = create_async_engine(get_settings().database_url)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _plant(
    db: Any,
    claim: AssignmentClaim,
    *,
    version: int,
    submitted_at: datetime,
    tier: int | None,
    drained: bool = True,
) -> Submission:
    submission = Submission(
        claim_id=claim.id,
        version=version,
        object_key=f"loadtest/{uuid4()}",
        original_filename="probe.csv",
        declared_type="CSV",
        file_size=10,
        submitted_at=submitted_at,
        retention_permanent=True,
        validation_status="VALIDATED" if drained else "UPLOADED",
    )
    db.add(submission)
    await db.flush()
    if drained:
        db.add(
            SubmissionValidation(
                submission_id=submission.id,
                parser_version="loadtest-probe",
                started_at=submitted_at,
                finished_at=submitted_at + timedelta(seconds=2),
                duration_ms=2000,
                status="VALIDATED",
            )
        )
    if tier is not None:
        db.add(
            RewardLockHistory(
                claim_id=claim.id,
                submission_id=submission.id,
                lock_status_from="NONE",
                lock_status_to="PROVISIONAL",
                reward_tier_locked=tier,
                locked_reward_points=100 * tier // 100,
                reason="probe",
            )
        )
    return submission


async def test_checker_refuses_a_world_missing_from_the_db(
    tmp_path: Path,
) -> None:
    """A vacuous pass is worse than a failure: if the world's task is not
    in the DB (re-seeded, or wiped by test cleanup), every count is zero
    and the checker must say so instead of smiling at 0 == 0."""

    world = await seed_world(
        students=3,
        claim_slots=2,
        race_users=1,
        race_slots=1,
        deadline_in_minutes=25,
        out_dir=tmp_path,
        force=True,
    )
    engine, maker = await _engine_maker()
    try:
        async with maker() as db:
            # the world exists; point the checker at a random task id
            stranger = dict(world, task_id=str(uuid4()))
            report = await run_checks(db, stranger, _summary(complete_ok=0))
        assert report["ok"] is False
        assert any("not found in DB" in v for v in report["violations"])
    finally:
        await _cleanup(world)


async def test_checker_green_on_clean_world(tmp_path: Path) -> None:
    world = await seed_world(
        students=3,
        claim_slots=2,
        race_users=1,
        race_slots=1,
        deadline_in_minutes=25,
        out_dir=tmp_path,
        force=True,
    )
    engine, maker = await _engine_maker()
    try:
        async with maker() as db:
            task = (
                await db.scalars(select(Task).where(Task.title == LOAD_TASK_TITLE))
            ).one()
            claim = (
                await db.scalars(
                    select(AssignmentClaim).where(AssignmentClaim.task_id == task.id)
                )
            ).first()
            deadline = claim.deadline_at
            await _plant(
                db,
                claim,
                version=1,
                submitted_at=deadline - timedelta(minutes=5),
                tier=100,
            )
            await db.commit()

            report = await run_checks(db, world, _summary(complete_ok=1))
        assert report["ok"] is True, report["violations"]
        assert report["metrics"]["submissions"] == 1
    finally:
        await _cleanup(world)


async def test_checker_flags_post_grace_submission(tmp_path: Path) -> None:
    world = await seed_world(
        students=3,
        claim_slots=2,
        race_users=1,
        race_slots=1,
        deadline_in_minutes=25,
        out_dir=tmp_path,
        force=True,
    )
    engine, maker = await _engine_maker()
    try:
        async with maker() as db:
            claim = await _first_claim(db)
            grace = claim.grace_deadline_at
            await _plant(
                db,
                claim,
                version=1,
                submitted_at=grace + timedelta(minutes=1),
                tier=20,
            )
            await db.commit()
            report = await run_checks(db, world, _summary(complete_ok=1))
        assert report["ok"] is False
        assert any("post-grace" in v for v in report["violations"])
    finally:
        await _cleanup(world)


async def test_checker_flags_summary_mismatch_and_wrong_tier(
    tmp_path: Path,
) -> None:
    world = await seed_world(
        students=3,
        claim_slots=2,
        race_users=1,
        race_slots=1,
        deadline_in_minutes=25,
        out_dir=tmp_path,
        force=True,
    )
    engine, maker = await _engine_maker()
    try:
        async with maker() as db:
            claim = await _first_claim(db)
            deadline = claim.deadline_at
            # pre-deadline submission but a WRONG tier row (80 instead of 100)
            await _plant(
                db,
                claim,
                version=1,
                submitted_at=deadline - timedelta(minutes=1),
                tier=80,
            )
            await db.commit()
            # summary claims 2 completes but DB holds 1 -> lost/mismatch
            report = await run_checks(db, world, _summary(complete_ok=2))
        assert report["ok"] is False
        assert any("mismatch" in v for v in report["violations"])
        assert any("tier" in v for v in report["violations"])
    finally:
        await _cleanup(world)


async def test_database_rejects_duplicate_claim_version(tmp_path: Path) -> None:
    """The UNIQUE(claim_id, version) constraint is the dup guarantee."""

    world = await seed_world(
        students=3,
        claim_slots=2,
        race_users=1,
        race_slots=1,
        deadline_in_minutes=25,
        out_dir=tmp_path,
        force=True,
    )
    engine, maker = await _engine_maker()
    try:
        async with maker() as db:
            claim = await _first_claim(db)
            first = await _plant(
                db,
                claim,
                version=1,
                submitted_at=claim.deadline_at - timedelta(minutes=1),
                tier=100,
            )
            await db.commit()

        # independent session (repo concurrency-test pattern): a second
        # writer inserting the same (claim, version) must be refused by
        # the database itself, not by any Python-side check
        async with maker() as db2:
            db2.add(
                Submission(
                    claim_id=claim.id,
                    version=1,
                    object_key=f"loadtest/{uuid4()}",
                    original_filename="dup.csv",
                    declared_type="CSV",
                    file_size=10,
                    submitted_at=datetime.now(UTC),
                    retention_permanent=True,
                )
            )
            with pytest.raises(IntegrityError):
                await db2.commit()
            await db2.rollback()
        assert first.version == 1
    finally:
        await _cleanup(world)


async def _first_claim(db: Any) -> AssignmentClaim:
    task = (await db.scalars(select(Task).where(Task.title == LOAD_TASK_TITLE))).one()
    return (
        await db.scalars(
            select(AssignmentClaim).where(AssignmentClaim.task_id == task.id)
        )
    ).first()


async def _cleanup(world: dict[str, Any]) -> None:
    engine, maker = await _engine_maker()
    async with maker() as db:
        task = await db.scalar(select(Task).where(Task.title == LOAD_TASK_TITLE))
        if task is not None:
            claim_ids = select(AssignmentClaim.id).where(
                AssignmentClaim.task_id == task.id
            )
            await db.execute(
                delete(RewardLockHistory).where(
                    RewardLockHistory.claim_id.in_(claim_ids)
                )
            )
            submission_ids = select(Submission.id).where(
                Submission.claim_id.in_(claim_ids)
            )
            await db.execute(
                delete(SubmissionValidation).where(
                    SubmissionValidation.submission_id.in_(submission_ids)
                )
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
