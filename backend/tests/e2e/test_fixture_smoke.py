# backend/tests/e2e/test_fixture_smoke.py
"""Fixture smoke (plan 10 task 1, step 1).

Proves the e2e world-building contract every later task module leans
on, in two halves:

1. ``test_seeds_independent_world`` — the three roles, one Task with
   three Assignments, one RewardItem all land committed with the
   promised shape, and every fixture id is independent (server UUIDs;
   pinned here so a future id-reuse regression fails loudly).
2. ``test_teardown_restores_clean_database`` — ``clean_world`` really
   returns the shared test database to clean: scoped counts over the
   key tables hit zero and no honor definition leaked.

Also exercises the clock-control seam end to end at smoke depth: the
SteppableClock invariants, the ``get_business_clock`` override install/
restore, and the real expiry-job trigger running at an explicit
advanced instant (its payloads reduce to the closed outcome set — the
scan answers, never exceptions).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.main import create_app
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import TotpCredential, User
from app.modules.points.models import RewardItem
from app.modules.tasks.enums import AssignmentAvailability, TaskStatus
from app.modules.tasks.models import Assignment, Task
from tests.e2e.clock_control import (
    SteppableClock,
    install_clock_override,
    trigger_expire_claims_scan,
)
from tests.e2e.factories import (
    RewardItemFixture,
    TaskFixture,
    UserFixture,
    clean_world,
    seed_admin,
    seed_reward_item,
    seed_student,
    seed_task_with_assignments,
    seed_teacher_confirmed_totp,
    snapshot_honor_ids,
)

pytestmark = pytest.mark.e2e

#: The closed answer set of the real expiry job (ClaimService's
#: ExpireOutcome members) — the scan's payloads must all reduce to it.
_EXPIRE_OUTCOMES = {
    "EXPIRED",
    "NOT_DUE",
    "PROTECTED",
    "VALID_SUBMISSION",
    "ALREADY_TERMINAL",
    "MISSING",
}


async def _seed_full_world(
    db_factory: async_sessionmaker[AsyncSession],
) -> tuple[str, UserFixture, UserFixture, UserFixture, TaskFixture, RewardItemFixture]:
    """The plan-step-1 world: three roles, one Task with three
    Assignments, one RewardItem — seeded committed through the real
    factories."""
    run = uuid.uuid4().hex[:12]
    student = await seed_student(db_factory, run=run)
    teacher = await seed_teacher_confirmed_totp(db_factory, run=run)
    admin = await seed_admin(db_factory, run=run)
    task = await seed_task_with_assignments(
        db_factory, teacher_id=teacher.user_id, run=run
    )
    item = await seed_reward_item(db_factory, run=run)
    return run, student, teacher, admin, task, item


async def test_seeds_independent_world(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    run, student, teacher, admin, task, item = await _seed_full_world(db_factory)
    honors_before = await snapshot_honor_ids(db_factory)
    try:
        # Fixture ids are independent: three users, one task, three
        # assignments, one reward item -> eight pairwise-distinct UUIDs.
        all_ids = [
            student.user_id,
            teacher.user_id,
            admin.user_id,
            task.task_id,
            *task.assignment_ids,
            item.reward_item_id,
        ]
        assert len(all_ids) == 8
        assert len(set(all_ids)) == 8

        # The committed shape matches each factory's promise.
        async with db_factory() as db:
            for fixture, role in (
                (student, Role.STUDENT),
                (teacher, Role.TEACHER),
                (admin, Role.ADMIN),
            ):
                row = await db.get(User, fixture.user_id)
                assert row is not None
                assert row.username == fixture.username
                assert row.role == role
                assert row.status == UserStatus.ACTIVE
            # The teacher's management guard: one CONFIRMED credential.
            totp = await db.scalar(
                select(TotpCredential).where(TotpCredential.user_id == teacher.user_id)
            )
            assert totp is not None and totp.confirmed_at is not None
            # Students carry the ACTIVE-rule phone; staff need none.
            student_row = await db.get(User, student.user_id)
            assert student_row is not None and student_row.phone_e164 is not None

            task_row = await db.get(Task, task.task_id)
            assert task_row is not None
            assert task_row.owner_teacher_id == teacher.user_id
            assert task_row.status == TaskStatus.PUBLISHED

            assignment_rows = (
                (
                    await db.execute(
                        select(Assignment).where(Assignment.task_id == task.task_id)
                    )
                )
                .scalars()
                .all()
            )
            assert {row.id for row in assignment_rows} == set(task.assignment_ids)
            assert all(
                row.availability_status == AssignmentAvailability.AVAILABLE
                for row in assignment_rows
            )

            item_row = await db.get(RewardItem, item.reward_item_id)
            assert item_row is not None
            assert item_row.enabled is True
            assert item_row.point_cost == item.point_cost

        # The clock seam at smoke depth: monotonic stepping, the API
        # dependency override round-trip, and the REAL expiry-job
        # trigger running at the advanced instant.
        start = datetime(2026, 5, 1, 8, 0, tzinfo=UTC)
        clock = SteppableClock(start)
        assert clock.now() == start
        clock.advance(timedelta(hours=25))
        assert clock.now() == start + timedelta(hours=25)
        with pytest.raises(ValueError):
            clock.advance_to(start)  # never backwards
        with pytest.raises(ValueError):
            clock.advance(timedelta(seconds=-1))
        naive = datetime(2026, 5, 3, 8, 0)
        with pytest.raises(ValueError):
            clock.advance_to(naive)  # aware-only, the FrozenClock rule

        app = create_app()
        restore = install_clock_override(app, clock)
        from app.modules.identity.dependencies import get_business_clock

        try:
            assert app.dependency_overrides[get_business_clock]() is clock
        finally:
            restore()
        # Only OUR override is gone — create_app itself installs an
        # unrelated get_role_bearer override, so "no overrides at all"
        # is not an invariant of a fresh app.
        assert get_business_clock not in app.dependency_overrides

        payloads = await trigger_expire_claims_scan(clock.now())
        assert isinstance(payloads, list)
        assert all(entry["outcome"] in _EXPIRE_OUTCOMES for entry in payloads)
    finally:
        await clean_world(
            db_factory,
            user_ids=[student.user_id, teacher.user_id, admin.user_id],
            task_ids=[task.task_id],
            reward_item_ids=[item.reward_item_id],
            honor_ids_before=honors_before,
        )


async def test_teardown_restores_clean_database(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, student, teacher, admin, task, item = await _seed_full_world(db_factory)
    honors_before = await snapshot_honor_ids(db_factory)
    await clean_world(
        db_factory,
        user_ids=[student.user_id, teacher.user_id, admin.user_id],
        task_ids=[task.task_id],
        reward_item_ids=[item.reward_item_id],
        honor_ids_before=honors_before,
    )
    user_ids = [student.user_id, teacher.user_id, admin.user_id]
    async with db_factory() as db:

        async def count(model: Any, *conditions: Any) -> int:
            return (
                await db.execute(
                    select(func.count()).select_from(model).where(*conditions)
                )
            ).scalar_one()

        assert await count(User, User.id.in_(user_ids)) == 0
        assert await count(TotpCredential, TotpCredential.user_id.in_(user_ids)) == 0
        assert await count(Task, Task.id == task.task_id) == 0
        assert await count(Assignment, Assignment.task_id == task.task_id) == 0
        assert await count(RewardItem, RewardItem.id == item.reward_item_id) == 0
        assert await snapshot_honor_ids(db_factory) == honors_before
