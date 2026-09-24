# backend/tests/e2e/test_ranking_recovery.py
"""Redis loss and ranking reconstruction gate (plan 10 task 8).

G7/G16 in one chain, everything real: the ledger rows live in
PostgreSQL (multi-period rewards PLUS a reversal — the §17.2
attribution rule under test), the boards are built by the REAL
``run_ranking_projection`` job entry, the four student-facing board
responses (daily / monthly / all-time / around-me) are captured
through the real API, then every ``ranking:*`` key is DELeted on the
real Redis, and the REAL ``run_rebuild_all_rankings`` job rebuilds the
projection from PostgreSQL alone. The four responses after the rebuild
must equal the captured ones EXACTLY — parsed-JSON equality, field by
field, including scores and tie ordering (the seeded same-day tie
makes the order observable).

One normalization rebuild runs BEFORE the capture: incremental
projections from earlier suites can leave ledger-less members in Redis
(the debris ``rebuild_all`` deletes by design), and exact equality is
the contract between TWO rebuild-consistent states — the rebuild's own
eviction semantics stay covered by the rankings suite.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.main import create_app
from app.modules.points.models import PointsLedger
from app.modules.rankings.periods import business_day, day_bounds
from app.workers.jobs.project_ranking_update import run_ranking_projection
from app.workers.jobs.rebuild_rankings import run_rebuild_all_rankings
from tests.e2e.factories import (
    clean_world,
    seed_claim,
    seed_student,
    seed_task_with_assignments,
    seed_teacher_confirmed_totp,
    snapshot_honor_ids,
)
from tests.e2e.test_happy_path import _mint_access_token

pytestmark = pytest.mark.e2e

_BROKER_QUEUE_KEY = "celery"

_BOARD_PATHS = (
    "/api/v1/rankings/daily",
    "/api/v1/rankings/monthly",
    "/api/v1/rankings/all",
    "/api/v1/rankings/around-me",
)


@pytest_asyncio.fixture
async def _stack() -> AsyncIterator[dict[str, Any]]:
    settings = get_settings()
    app = create_app()
    broker = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        async with app.router.lifespan_context(app):
            yield {"app": app, "broker": broker, "settings": settings}
    finally:
        with contextlib.suppress(Exception):
            await broker.delete(_BROKER_QUEUE_KEY)
        await broker.aclose()


async def _seed_ledger(
    db_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    amount: int,
    effective_at: datetime,
    ledger_type: str = "ADMIN_ADJUSTMENT",
    source_id: UUID | None = None,
    reversal_of_id: UUID | None = None,
) -> UUID:
    """One committed ranking-affecting ledger row (the reward-shape
    column discipline of ``factories.seed_points_balance``, with the
    effective instant and type under the test's control)."""
    async with db_factory() as db:
        entry = PointsLedger(
            user_id=user_id,
            ledger_type=ledger_type,
            amount=amount,
            source_type="ASSIGNMENT_CLAIM"
            if ledger_type.startswith("ASSIGNMENT")
            else "ADMIN_ADJUSTMENT",
            source_id=source_id if source_id is not None else uuid.uuid4(),
            affects_balance=True,
            affects_ranking=True,
            ranking_effective_at=effective_at,
            reversal_of_id=reversal_of_id,
            reason="e2e 排名重建种子" if ledger_type == "ADMIN_ADJUSTMENT" else None,
        )
        db.add(entry)
        await db.commit()
        return entry.id


async def _delete_ledger_rows(
    db_factory: async_sessionmaker[AsyncSession], user_ids: list[UUID]
) -> None:
    async with db_factory() as db:
        # The self-referencing reversal FK is NO ACTION: deleting a
        # reward row while THIS run's reversal still points at it (ORM
        # unit-of-work order is insertion order, and the reward is
        # inserted first) violates the constraint. Sever the pointers
        # first — scoped to this run's users only.
        await db.execute(
            update(PointsLedger)
            .where(
                PointsLedger.user_id.in_(user_ids),
                PointsLedger.reversal_of_id.is_not(None),
            )
            .values(reversal_of_id=None)
        )
        rows = (
            (
                await db.execute(
                    select(PointsLedger).where(PointsLedger.user_id.in_(user_ids))
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            await db.delete(row)
        await db.commit()


async def test_rankings_rebuild_exactly_after_redis_loss(
    db_factory: async_sessionmaker[AsyncSession], _stack: dict[str, Any]
) -> None:
    """Multi-period rewards + a reversal -> four board responses ->
    DEL ranking:* -> the real rebuild job -> the four responses are
    byte-for-field equal, scores and tie order included."""
    settings = _stack["settings"]
    tz = ZoneInfo(settings.business_timezone)
    now = datetime.now(UTC)

    # Period instants in the BUSINESS timezone: today (midday local —
    # always inside the current business day), exactly one local day
    # back (24h has no DST in Shanghai), and a date inside the
    # PREVIOUS business month (the 1st minus 10 days at local noon).
    today_start, _today_end = day_bounds(business_day(now, tz), tz)
    t_today = today_start + timedelta(hours=12)
    t_yesterday = now - timedelta(hours=24)
    first_of_month = now.astimezone(tz).replace(
        day=1, hour=12, minute=0, second=0, microsecond=0
    )
    t_prev_month = (first_of_month - timedelta(days=10)).astimezone(UTC)

    run = uuid.uuid4().hex[:12]
    honors_before = await snapshot_honor_ids(db_factory)
    teacher = await seed_teacher_confirmed_totp(db_factory, run=f"t{run}")
    students = [
        await seed_student(db_factory, run=f"{index}{run}") for index in range(3)
    ]
    student_a, student_b, student_c = students
    task = await seed_task_with_assignments(
        db_factory, teacher_id=teacher.user_id, run=run, assignment_count=3
    )
    headers_a = {
        "Authorization": (
            f"Bearer {await _mint_access_token(db_factory, student_a.user_id)}"
        )
    }

    # Rewards: A spans all three periods; B carries the §17.2 reversal
    # (a later-posted row attributing back to the ORIGINAL month); C
    # ties A on today's board (tie ordering must survive the rebuild).
    # ``seed_claim`` claims ``assignment_ids[0]`` of the fixture it is
    # handed, so each student gets a single-assignment view of the task.
    import dataclasses

    def _view(index: int) -> Any:
        return dataclasses.replace(task, assignment_ids=[task.assignment_ids[index]])

    claim_a = await seed_claim(db_factory, task=_view(0), student_id=student_a.user_id)
    claim_b = await seed_claim(db_factory, task=_view(1), student_id=student_b.user_id)
    reward_b = await _seed_ledger(
        db_factory,
        user_id=student_b.user_id,
        amount=100,
        effective_at=t_prev_month,
        ledger_type="ASSIGNMENT_REWARD",
        source_id=claim_b.claim_id,
    )
    entries: list[tuple[UUID, datetime]] = [
        (
            await _seed_ledger(
                db_factory,
                user_id=student_a.user_id,
                amount=100,
                effective_at=t_prev_month,
                ledger_type="ASSIGNMENT_REWARD",
                source_id=claim_a.claim_id,
            ),
            t_prev_month,
        ),
        (
            await _seed_ledger(
                db_factory,
                user_id=student_a.user_id,
                amount=50,
                effective_at=t_yesterday,
            ),
            t_yesterday,
        ),
        (
            await _seed_ledger(
                db_factory, user_id=student_a.user_id, amount=30, effective_at=t_today
            ),
            t_today,
        ),
        # The reversal: posted "now"-ish, EFFECTIVE in the original
        # month (the attribution rule), so B nets 60 all-time and 60 in
        # the previous month while this month stays untouched.
        (
            await _seed_ledger(
                db_factory,
                user_id=student_b.user_id,
                amount=-40,
                effective_at=t_prev_month,
                ledger_type="ASSIGNMENT_REWARD_REVERSAL",
                source_id=claim_b.claim_id,
                reversal_of_id=reward_b,
            ),
            t_prev_month,
        ),
        (
            await _seed_ledger(
                db_factory,
                user_id=student_c.user_id,
                amount=80,
                effective_at=t_yesterday,
            ),
            t_yesterday,
        ),
        (
            await _seed_ledger(
                db_factory, user_id=student_c.user_id, amount=30, effective_at=t_today
            ),
            t_today,
        ),
    ]
    # The original +100 for B is also part of the projection input.
    entries.append((reward_b, t_prev_month))

    broker: aioredis.Redis = _stack["broker"]
    try:
        # The REAL per-entry projection job (absolute recompute per
        # affected period — the reward flow's own enqueue shape).
        for entry_id, effective_at in entries:
            user_id = await _user_of(db_factory, entry_id)
            summary = await asyncio.to_thread(
                run_ranking_projection,
                str(user_id),
                effective_at.isoformat(),
                request_id=f"e2e-rank-project-{run}",
            )
            assert summary["updated_keys"], summary

        # Normalize once (see module docstring), then capture.
        normalized = await asyncio.to_thread(
            run_rebuild_all_rankings, request_id=f"e2e-rank-normalize-{run}"
        )
        assert normalized["members_written"] >= len(entries)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_stack["app"]),
            base_url="http://e2e-ranking",
        ) as client:
            captured: dict[str, Any] = {}
            for path in _BOARD_PATHS:
                response = await client.get(path, headers=headers_a)
                assert response.status_code == 200, (path, response.text)
                captured[path] = response.json()

        # Sanity: the seeded facts are ON the captured boards (A ties C
        # today at 30; B's net is 60 everywhere; the previous month's
        # board for B also reads 60 — the reversal attribution).
        today_entries = captured["/api/v1/rankings/daily"]["entries"]
        today_scores = {(entry["nickname"], entry["score"]) for entry in today_entries}
        assert any(score == 30 for _, score in today_scores), today_scores
        assert captured["/api/v1/rankings/all"]["my_score"] == 180

        # The loss: every ranking key really disappears.
        keys_before = [key async for key in broker.scan_iter(match="ranking:*")]
        assert keys_before, "the boards must exist before the loss"
        if keys_before:
            await broker.delete(*keys_before)
        remaining = [key async for key in broker.scan_iter(match="ranking:*")]
        assert remaining == [], remaining

        # The REAL rebuild job, from PostgreSQL alone.
        rebuilt = await asyncio.to_thread(
            run_rebuild_all_rankings, request_id=f"e2e-rank-rebuild-{run}"
        )
        assert rebuilt["members_written"] >= len(entries)
        assert any(key.startswith("ranking:daily:") for key in rebuilt["keys_rebuilt"])

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_stack["app"]),
            base_url="http://e2e-ranking",
        ) as client:
            for path in _BOARD_PATHS:
                response = await client.get(path, headers=headers_a)
                assert response.status_code == 200, (path, response.text)
                assert response.json() == captured[path], (
                    path,
                    response.json(),
                    captured[path],
                )
    finally:
        # Boards: remove only THIS run's members (never another
        # suite's); emptied ZSETs vanish on their own.
        member_ids = [str(student.user_id) for student in students]
        async for key in broker.scan_iter(match="ranking:*"):
            await broker.zrem(key, *member_ids)
        await _delete_ledger_rows(db_factory, [student.user_id for student in students])
        await clean_world(
            db_factory,
            user_ids=[teacher.user_id, *(student.user_id for student in students)],
            task_ids=[task.task_id],
            honor_ids_before=honors_before,
        )


async def _user_of(
    db_factory: async_sessionmaker[AsyncSession], entry_id: UUID
) -> UUID:
    async with db_factory() as db:
        row = await db.get(PointsLedger, entry_id)
        assert row is not None
        return row.user_id
