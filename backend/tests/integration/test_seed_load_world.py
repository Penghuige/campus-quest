"""Integration: the load-world seeder builds a valid, token-ready world.

Drives ``loadtest.seed.seed_world`` against the compose PostgreSQL and
asserts the world shape the scenarios depend on (spec §Scenarios): a
PUBLISHED FIXED-deadline hot task, pre-seeded claims with all five §6.2
snapshots, and offline-minted tokens that really authenticate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db.session import get_db_session
from app.main import create_app
from app.modules.identity.models import User, UserSession
from app.modules.tasks.claim_service import REWARD_POLICY_SNAPSHOT_V1
from app.modules.tasks.enums import (
    AssignmentAvailability,
    DeadlineMode,
    TaskStatus,
)
from app.modules.tasks.models import Assignment, AssignmentClaim, Task
from loadtest.seed import LOAD_TASK_TITLE, LoadWorldExistsError, seed_world

pytestmark = pytest.mark.integration


async def test_seed_world_builds_publishable_task_with_pre_seeded_claims(
    tmp_path: Path,
) -> None:
    # force=True: the shared DB may carry a load world from a live run
    world = await seed_world(
        students=3,
        claim_slots=2,
        race_users=1,
        race_slots=1,
        deadline_in_minutes=25,
        out_dir=tmp_path,
        force=True,
    )
    try:
        from sqlalchemy.ext.asyncio import (
            async_sessionmaker,
            create_async_engine,
        )

        settings = get_settings()
        engine = create_async_engine(settings.database_url)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db:
            task = (
                await db.scalars(select(Task).where(Task.title == LOAD_TASK_TITLE))
            ).one()
            assert task.status == TaskStatus.PUBLISHED
            assert task.deadline_mode == DeadlineMode.FIXED
            assert set(task.allowed_file_types) == {"CSV", "XLSX", "SQLITE"}
            assert task.fixed_deadline_at is not None
            # the run window: ~25 minutes out (cutoff=0 publishes the
            # tight deadline directly; tolerance covers test runtime)
            delta_min = (
                task.fixed_deadline_at - datetime.now(UTC)
            ).total_seconds() / 60
            assert 20 < delta_min <= 25

            assignments = (
                await db.scalars(
                    select(Assignment)
                    .where(Assignment.task_id == task.id)
                    .order_by(Assignment.keyword)
                )
            ).all()
            assert len(assignments) == 3  # claim_slots + race_slots
            occupied = [
                a
                for a in assignments
                if a.availability_status == AssignmentAvailability.OCCUPIED
            ]
            assert len(occupied) == 2

            claims = (
                await db.scalars(
                    select(AssignmentClaim).where(AssignmentClaim.task_id == task.id)
                )
            ).all()
            assert len(claims) == 2
            for claim in claims:
                # §6.2 MUST-list: all five snapshots carried
                assert claim.deadline_at == task.fixed_deadline_at
                assert claim.grace_deadline_at is not None
                assert claim.reward_policy_snapshot == REWARD_POLICY_SNAPSHOT_V1
                assert claim.base_reward_points_snapshot == task.base_reward_points
                assert claim.submission_schema_version is not None
        await engine.dispose()

        # world.json: S1 users carry claim ids, racers do not
        assert (tmp_path / "world.json").is_file()
        assert world["slots_total"] == 3
        with_claim = [u for u in world["users"] if u["claim_id"] is not None]
        without = [u for u in world["users"] if u["claim_id"] is None]
        assert len(with_claim) == 2
        assert len(without) == 1
        assert all(u["access_token"] for u in world["users"])
    finally:
        await _cleanup(world)


async def test_seed_world_tokens_authenticate(tmp_path: Path) -> None:
    world: dict[str, Any] | None = None
    try:
        world = await seed_world(
            students=3,
            claim_slots=2,
            race_users=1,
            race_slots=1,
            deadline_in_minutes=25,
            out_dir=tmp_path,
            force=True,
        )
        user = world["users"][0]
        engine = create_async_engine(get_settings().database_url)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as db:
                # Repo app-test harness pattern (test_platforms_api.py):
                # override get_db_session with THIS loop's session. The
                # app engine is process-wide lru_cache'd (app/db/session.py)
                # — in the full CI suite a smoke test binds its pool to a
                # since-closed loop, and an un-overridden app then reuses
                # a dead-loop connection ("Future attached to a different
                # loop", PR #153 CI). /me is read-only, so sharing the
                # session commits nothing.
                app = create_app()

                async def _test_db_session() -> AsyncIterator[AsyncSession]:
                    yield db

                app.dependency_overrides[get_db_session] = _test_db_session
                transport = ASGITransport(app=app)
                async with AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    response = await client.get(
                        "/api/v1/me",
                        headers={"Authorization": f"Bearer {user['access_token']}"},
                    )
                assert response.status_code == 200
                assert response.json()["username"] == user["username"]
        finally:
            await engine.dispose()
    finally:
        if world is not None:
            await _cleanup(world)


async def test_seed_world_refuses_without_force(tmp_path: Path) -> None:
    world: dict[str, Any] | None = None
    try:
        # force=True first: the shared DB may carry a load world from a
        # live run, and this test's subject is the SECOND seed's refusal
        world = await seed_world(
            students=3,
            claim_slots=2,
            race_users=1,
            race_slots=1,
            deadline_in_minutes=25,
            out_dir=tmp_path,
            force=True,
        )
        with pytest.raises(LoadWorldExistsError):
            await seed_world(
                students=3,
                claim_slots=2,
                race_users=1,
                race_slots=1,
                deadline_in_minutes=25,
                out_dir=tmp_path,
            )
        # force replaces the world (still exactly one load task)
        await seed_world(
            students=3,
            claim_slots=2,
            race_users=1,
            race_slots=1,
            deadline_in_minutes=25,
            out_dir=tmp_path,
            force=True,
        )
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        settings = get_settings()
        engine = create_async_engine(settings.database_url)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db:
            tasks = (
                await db.scalars(select(Task).where(Task.title == LOAD_TASK_TITLE))
            ).all()
            assert len(tasks) == 1
        await engine.dispose()
    finally:
        if world is not None:
            await _cleanup(world)


async def test_seed_world_force_replaces_after_submissions(
    tmp_path: Path,
) -> None:
    """Regression (Task 5 dry-run triage): --force must drop a world whose
    claims already carry submissions and downstream rows, not die on FKs."""

    world = await seed_world(
        students=3,
        claim_slots=2,
        race_users=1,
        race_slots=1,
        deadline_in_minutes=25,
        out_dir=tmp_path,
    )
    try:
        from sqlalchemy.ext.asyncio import (
            async_sessionmaker,
            create_async_engine,
        )

        from app.modules.submissions.models import Submission

        claim_id = next(
            u["claim_id"] for u in world["users"] if u["claim_id"] is not None
        )
        settings = get_settings()
        engine = create_async_engine(settings.database_url)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db:
            claim = await db.get(AssignmentClaim, UUID(claim_id))
            db.add(
                Submission(
                    claim_id=claim.id,
                    version=1,
                    object_key="loadtest/stale-probe",
                    original_filename="stale.csv",
                    declared_type="CSV",
                    file_size=10,
                    submitted_at=datetime.now(UTC),
                    # retention CHECK: permanent keeps retention_until NULL
                    retention_permanent=True,
                )
            )
            await db.commit()
        await engine.dispose()

        replaced = await seed_world(
            students=3,
            claim_slots=2,
            race_users=1,
            race_slots=1,
            deadline_in_minutes=25,
            out_dir=tmp_path,
            force=True,
        )
        assert replaced["task_id"] != world["task_id"]
    finally:
        await _cleanup(world)


async def _cleanup(world: dict[str, Any]) -> None:
    """Remove this test's world rows so the shared DB stays clean."""

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        task = await db.scalar(select(Task).where(Task.title == LOAD_TASK_TITLE))
        if task is not None:
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
