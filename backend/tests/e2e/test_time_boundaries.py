# backend/tests/e2e/test_time_boundaries.py
"""Exact time-boundary matrix (plan 10 task 4; quality gate G14: time
boundaries are an API -- server-authoritative, timezone-aware, with
explicit ``<``/``<=`` choices and exactly-at-boundary behavior pinned).

Same real-wiring posture as ``test_happy_path`` / ``test_deadline_flows``
(zero core provider overrides) on the task-1 seams: a ``SteppableClock``
behind ``get_business_clock`` so every deadline judgement reads one
exact stepped instant, and the real worker cores (validation entry,
ranking projection entry) driven at explicit instants.

Three groups:

1. **Reward ladder ±1ms matrix** (spec §9.3): every tier edge --
   deadline, +4h, +12h, grace -- probed at exactly the boundary AND
   1ms on each side through the real claim -> presign -> PUT ->
   finalize -> validation -> approve chain, asserting the locked tier,
   the exact integer points (Decimal-floor, never a float artifact),
   and the typed ``SUBMISSION_WINDOW_CLOSED`` rejection at/after
   grace. Tier fractions AND tier widths come from the ladder
   implementation (``deadlines.py``'s constants), so the matrix cannot
   drift from the code it pins.
2. **Business-timezone rollover**: ranking day/month attribution and
   the abandon daily quota grouped at the local ``23:59:59.999`` /
   ``00:00:00`` boundary. Ledger entries are seeded at the exact
   boundary instants (the e1 world-building charter), projected by the
   REAL ranking job, and read back through the REAL board routes at
   the same stepped instants; the quota case drives the REAL abandon
   API with the daily cap straddling local midnight.
3. **DST zone-awareness**: business timezone flipped to
   ``America/New_York`` through existing seams only -- the FastAPI
   ``get_settings`` dependency for the routes, and the projection
   job's injectable ``tz`` constructor for the worker. The
   spring-forward day (2026-03-08, 02:00->03:00 EST->EDT) groups its
   board by the 23-hour local day: an instant that fixed-+8
   arithmetic would bucket into the NEXT day lands on this one --
   proving no hardcoded offset anywhere in the grouping path.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.points.models import PointsLedger, PointWallet
from app.modules.rankings.periods import (
    ALL_TIME_KEY,
    business_day,
    business_month,
    daily_key,
    day_bounds,
    monthly_key,
)
from app.modules.rankings.redis_projection import RankingRedisProjection
from app.modules.submissions.enums import FileType
from app.modules.submissions.models import UploadIntent
from app.modules.tasks import deadlines
from app.modules.tasks.enums import ClaimStatus, RewardLockStatus
from app.modules.tasks.models import AssignmentClaim
from app.workers.jobs.project_ranking_update import run_ranking_projection
from tests.e2e.clock_control import SteppableClock, install_clock_override
from tests.e2e.factories import (
    clean_world,
    seed_student,
    seed_task_with_assignments,
    seed_teacher_confirmed_totp,
    snapshot_honor_ids,
)
from tests.e2e.test_deadline_flows import _claim, _submit, _validate
from tests.e2e.test_happy_path import _mint_access_token, _purge_objects

pytestmark = pytest.mark.e2e

_BROKER_QUEUE_KEY = "celery"

#: A 60-minute RELATIVE task: deadline = claim + 1h, grace = +24h -- the
#: matrix instants are exact offsets from that deadline.
_DURATION_MINUTES = 60

#: The ladder's own constants (single-sourced from ``deadlines.py``):
#: tier fractions for the expected values, the private tier widths and
#: grace default for the probe offsets. Importing the privates is
#: deliberate -- the matrix must probe exactly the implementation's
#: edges, not a restated copy of them.
_FRACTION_FULL = deadlines.FRACTION_FULL
_FRACTION_EARLY = deadlines.FRACTION_EARLY
_FRACTION_MID = deadlines.FRACTION_MID
_FRACTION_LATE = deadlines.FRACTION_LATE
_EARLY_WIDTH = deadlines._EARLY_TIER_WIDTH  # noqa: SLF001 - see above
_MID_WIDTH = deadlines._MID_TIER_WIDTH  # noqa: SLF001 - see above
_GRACE_WIDTH = timedelta(
    minutes=deadlines._GRACE_PERIOD_DEFAULT_MINUTES  # noqa: SLF001
)
_MS = timedelta(milliseconds=1)

#: The exact-instant boundary matrix: offset from the deadline, and the
#: tier fraction the ladder must lock there (``None`` = the window is
#: closed -- the typed rejection case). Every edge is probed on BOTH
#: sides plus exactly at the point (G14: exactly-at-boundary is an API).
_BOUNDARY_CASES = [
    pytest.param(-_MS, _FRACTION_FULL, id="deadline_minus_1ms"),
    pytest.param(timedelta(0), _FRACTION_FULL, id="deadline_exact"),
    pytest.param(_MS, _FRACTION_EARLY, id="deadline_plus_1ms"),
    pytest.param(_EARLY_WIDTH - _MS, _FRACTION_EARLY, id="plus_4h_minus_1ms"),
    pytest.param(_EARLY_WIDTH, _FRACTION_MID, id="plus_4h_exact"),
    pytest.param(_EARLY_WIDTH + _MS, _FRACTION_MID, id="plus_4h_plus_1ms"),
    pytest.param(_MID_WIDTH - _MS, _FRACTION_MID, id="plus_12h_minus_1ms"),
    pytest.param(_MID_WIDTH, _FRACTION_LATE, id="plus_12h_exact"),
    pytest.param(_MID_WIDTH + _MS, _FRACTION_LATE, id="plus_12h_plus_1ms"),
    pytest.param(_GRACE_WIDTH - _MS, _FRACTION_LATE, id="grace_minus_1ms"),
    pytest.param(_GRACE_WIDTH, None, id="grace_exact_closed"),
    pytest.param(_GRACE_WIDTH + _MS, None, id="grace_plus_1ms_closed"),
]

_GOOD_CSV = (
    b"url,title\n"
    b"https://example.com/note/1,\xe7\xac\xac\xe4\xb8\x80\xe6\x9d\xa1\n"
    b"https://example.com/note/2,\xe7\xac\xac\xe4\xba\x8c\xe6\x9d\xa1\n"
)

_BASE_POINTS = 100  # the factory's task base; integer-points contract


class _World:
    """One run's seeded ids (the deadline-flows bookkeeping shape)."""

    def __init__(self, run: str) -> None:
        self.run = run
        self.user_ids: list[UUID] = []
        self.task_ids: list[UUID] = []
        self.honors_before: set[UUID] | None = None


@asynccontextmanager
async def _stack_at(start: datetime) -> AsyncIterator[dict[str, Any]]:
    """App under lifespan + stepped clock + broker, from an explicit
    start instant (each test group needs its own calendar window; the
    clock only moves forward). Process-wide singleton resets stay in
    the e2e conftest; teardown restores the clock override and the
    broker queue this run published onto."""
    app = create_app()
    clock = SteppableClock(start)
    restore = install_clock_override(app, clock)
    broker = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        async with app.router.lifespan_context(app):
            yield {"app": app, "clock": clock, "broker": broker}
    finally:
        restore()
        with contextlib.suppress(Exception):
            await broker.delete(_BROKER_QUEUE_KEY)
        await broker.aclose()


async def _seed_one_student_world(
    db_factory: async_sessionmaker[AsyncSession],
    world: _World,
    *,
    assignment_count: int = 2,
    duration_minutes: int = _DURATION_MINUTES,
) -> tuple[dict[str, str], dict[str, str], str]:
    """Teacher + one student + one task; returns the teacher/student
    bearer headers and the task id (the matrix's per-case world)."""
    teacher = await seed_teacher_confirmed_totp(db_factory, run=world.run)
    world.user_ids.append(teacher.user_id)
    student = await seed_student(db_factory, run=f"0{world.run}")
    world.user_ids.append(student.user_id)
    task = await seed_task_with_assignments(
        db_factory,
        teacher_id=teacher.user_id,
        run=world.run,
        assignment_count=assignment_count,
        duration_minutes=duration_minutes,
    )
    world.task_ids.append(task.task_id)
    world.honors_before = await snapshot_honor_ids(db_factory)
    teacher_token = await _mint_access_token(db_factory, teacher.user_id)
    teacher_headers = {"Authorization": f"Bearer {teacher_token}"}
    student_headers = {
        "Authorization": (
            f"Bearer {await _mint_access_token(db_factory, student.user_id)}"
        )
    }
    return teacher_headers, student_headers, str(task.task_id)


async def _teardown_world(
    db_factory: async_sessionmaker[AsyncSession], world: _World
) -> None:
    """Purge S3 objects, this run's audit rows, and the seeded graph in
    FK order (the deadline-flows teardown discipline)."""
    await _purge_objects(db_factory, world.task_ids)
    async with db_factory() as db:
        await db.execute(
            delete(AuditLog).where(AuditLog.actor_user_id.in_(world.user_ids))
        )
        await db.commit()
    await clean_world(
        db_factory,
        user_ids=world.user_ids,
        task_ids=world.task_ids,
        honor_ids_before=world.honors_before,
    )


async def _load_claim(
    db_factory: async_sessionmaker[AsyncSession], claim_id: UUID
) -> AssignmentClaim:
    async with db_factory() as db:
        row = await db.get(AssignmentClaim, claim_id)
    assert row is not None
    return row


# --- group 1: the reward-ladder ±1ms matrix ------------------------------------------


@pytest.mark.parametrize(("offset", "expected_fraction"), _BOUNDARY_CASES)
async def test_reward_ladder_locks_exact_tier_at_every_boundary(
    db_factory: async_sessionmaker[AsyncSession],
    offset: timedelta,
    expected_fraction: Decimal | None,
) -> None:
    """One full real chain per boundary point: claim at T0, submit at
    exactly ``deadline + offset``, and the locked tier / granted integer
    points must equal the ladder constants. At/after grace the FIRST
    gate (upload-intent creation) answers the typed
    SUBMISSION_WINDOW_CLOSED and nothing is written."""
    # 2026-09-21 08:00Z: a neutral Monday, nothing else shares it.
    async with _stack_at(datetime(2026, 9, 21, 8, 0, tzinfo=UTC)) as stack:
        clock: SteppableClock = stack["clock"]
        world = _World(uuid.uuid4().hex[:12])
        teacher_headers, student_headers, task_id = await _seed_one_student_world(
            db_factory, world
        )
        try:
            transport = httpx.ASGITransport(app=stack["app"])
            async with httpx.AsyncClient(
                transport=transport, base_url="http://e2e-boundary"
            ) as client:
                claim_id = await _claim(client, student_headers, task_id)
                claim_uuid = uuid.UUID(claim_id)
                claim_row = await _load_claim(db_factory, claim_uuid)
                deadline = claim_row.deadline_at
                assert deadline is not None
                # Self-anchoring pre-assert: the persisted grace width is
                # the ladder's own default, so the grace probes below sit
                # exactly on the implementation's edge.
                assert claim_row.grace_deadline_at == deadline + _GRACE_WIDTH

                submit_at = deadline + offset
                clock.advance_to(submit_at)

                if expected_fraction is None:
                    intent = await client.post(
                        "/api/v1/submissions/upload-intent",
                        headers=student_headers,
                        json={
                            "claim_id": claim_id,
                            "filename": "边界.csv",
                            "declared_type": FileType.CSV.value,
                            "size": len(_GOOD_CSV),
                        },
                    )
                    assert intent.status_code == 409, intent.text
                    assert intent.json()["error"]["code"] == "SUBMISSION_WINDOW_CLOSED"
                    # Nothing was written: no intent, no submission, no
                    # lock -- the claim is exactly as claimed.
                    after = await _load_claim(db_factory, claim_uuid)
                    assert after.status == ClaimStatus.CLAIMED.value
                    assert after.latest_submission_id is None
                    assert after.reward_lock_status == RewardLockStatus.NONE.value
                    async with db_factory() as db:
                        intents = (
                            await db.execute(
                                select(UploadIntent.id).where(
                                    UploadIntent.claim_id == claim_uuid
                                )
                            )
                        ).all()
                    assert intents == []
                    return

                expected_points = deadlines.reward_points(
                    _BASE_POINTS, expected_fraction
                )
                assert isinstance(expected_points, int)

                submission_id = await _submit(
                    client,
                    student_headers,
                    claim_id,
                    _GOOD_CSV,
                    filename="边界.csv",
                )
                await _validate(submission_id, world.run)
                locked = await _load_claim(db_factory, claim_uuid)
                assert locked.reward_tier_locked == expected_points
                assert locked.locked_reward_points == expected_points
                assert locked.reward_lock_status == RewardLockStatus.PROVISIONAL.value

                approved = await client.post(
                    f"/api/v1/teacher/submissions/{submission_id}/approve",
                    headers=teacher_headers,
                )
                assert approved.status_code == 200, approved.text
                decision = approved.json()
                assert decision["points_granted"] == expected_points
                assert isinstance(decision["points_granted"], int)
                assert decision["claim_status"] == ClaimStatus.COMPLETED.value
                assert (
                    decision["reward_lock_status"] == RewardLockStatus.CONFIRMED.value
                )

            async with db_factory() as db:
                wallet = await db.get(PointWallet, world.user_ids[1])
                assert wallet is not None
                assert wallet.available_points == expected_points
                assert wallet.earned_points == expected_points
        finally:
            await _teardown_world(db_factory, world)


# --- group 2: business-timezone rollover ---------------------------------------------


async def _seed_ranking_entry(
    db_factory: async_sessionmaker[AsyncSession],
    *,
    student_id: UUID,
    amount: int,
    ranking_effective_at: datetime,
) -> None:
    """One ranking-affecting ledger entry at an EXACT instant (the
    factory's world-building shape with the effective time pinned --
    the boundary under test is the attribution instant, not "now")."""
    async with db_factory() as db:
        db.add(
            PointsLedger(
                user_id=student_id,
                ledger_type="ASSIGNMENT_REWARD",
                amount=amount,
                source_type="ASSIGNMENT_CLAIM",
                source_id=uuid.uuid4(),
                affects_balance=True,
                affects_ranking=True,
                ranking_effective_at=ranking_effective_at,
            )
        )
        await db.commit()


async def _board(
    client: httpx.AsyncClient, headers: dict[str, str], path: str
) -> dict[str, Any]:
    response = await client.get(f"/api/v1{path}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_ranking_periods_roll_at_local_day_and_month_boundary(
    db_factory: async_sessionmaker[AsyncSession], _tz_from_settings: ZoneInfo
) -> None:
    """Two ledger entries 1ms apart across local midnight of a month's
    last day: the REAL projection job buckets them into different
    daily AND monthly keys, and the REAL board routes -- addressed by
    the stepped clock and the Settings business timezone -- read each
    instant's own period back."""
    tz = _tz_from_settings
    # Local 2026-09-30 23:59:59.999 and 2026-10-01 00:00:00: the 1ms
    # step crosses the day AND the month boundary simultaneously.
    last_ms = datetime(2026, 9, 30, 23, 59, 59, 999000, tzinfo=tz)
    first = datetime(2026, 10, 1, 0, 0, 0, tzinfo=tz)
    late_instant, next_instant = last_ms.astimezone(UTC), first.astimezone(UTC)
    assert next_instant - late_instant == _MS

    async with _stack_at(datetime(2026, 9, 30, 12, 0, tzinfo=UTC)) as stack:
        clock: SteppableClock = stack["clock"]
        broker: aioredis.Redis = stack["broker"]
        world = _World(uuid.uuid4().hex[:12])
        student = await seed_student(db_factory, run=f"0{world.run}")
        world.user_ids.append(student.user_id)
        member = str(student.user_id)
        touched_keys: set[str] = set()
        try:
            async with db_factory() as db:
                db.add(
                    PointWallet(
                        user_id=student.user_id,
                        available_points=100,
                        earned_points=100,
                    )
                )
                await db.commit()
            await _seed_ranking_entry(
                db_factory,
                student_id=student.user_id,
                amount=30,
                ranking_effective_at=late_instant,
            )
            await _seed_ranking_entry(
                db_factory,
                student_id=student.user_id,
                amount=70,
                ranking_effective_at=next_instant,
            )

            # The REAL projection job (production defaults: the Settings
            # business timezone), one call per entry's own attribution.
            first_job = await asyncio.to_thread(
                run_ranking_projection,
                member,
                late_instant.isoformat(),
                request_id=f"e2e-boundary-roll-late-{world.run}",
            )
            second_job = await asyncio.to_thread(
                run_ranking_projection,
                member,
                next_instant.isoformat(),
                request_id=f"e2e-boundary-roll-next-{world.run}",
            )
            expected_late = {
                daily_key(business_day(late_instant, tz)),
                monthly_key(business_month(late_instant, tz)),
                ALL_TIME_KEY,
            }
            expected_next = {
                daily_key(business_day(next_instant, tz)),
                monthly_key(business_month(next_instant, tz)),
                ALL_TIME_KEY,
            }
            assert expected_late != expected_next  # day AND month differ
            assert set(first_job["updated_keys"]) == expected_late
            assert set(second_job["updated_keys"]) == expected_next
            touched_keys |= expected_late | expected_next
            for key, score in (
                (daily_key(business_day(late_instant, tz)), 30.0),
                (daily_key(business_day(next_instant, tz)), 70.0),
            ):
                assert await broker.zscore(key, member) == score

            # The REAL board routes at the same stepped instants.
            headers = {
                "Authorization": (
                    f"Bearer {await _mint_access_token(db_factory, student.user_id)}"
                )
            }
            transport = httpx.ASGITransport(app=stack["app"])
            async with httpx.AsyncClient(
                transport=transport, base_url="http://e2e-boundary"
            ) as client:
                clock.advance_to(late_instant)
                late_daily = await _board(client, headers, "/rankings/daily")
                late_monthly = await _board(client, headers, "/rankings/monthly")
                assert late_daily["my_score"] == 30
                assert late_monthly["my_score"] == 30

                clock.advance_to(next_instant)
                next_daily = await _board(client, headers, "/rankings/daily")
                next_monthly = await _board(client, headers, "/rankings/monthly")
                all_time = await _board(client, headers, "/rankings/all")
                assert next_daily["my_score"] == 70
                assert next_monthly["my_score"] == 70
                assert all_time["my_score"] == 100
        finally:
            for key in touched_keys:
                with contextlib.suppress(Exception):
                    await stack["broker"].zrem(key, member)
            await clean_world(db_factory, user_ids=world.user_ids)


async def test_abandon_daily_quota_groups_by_local_business_day(
    db_factory: async_sessionmaker[AsyncSession], _tz_from_settings: ZoneInfo
) -> None:
    """The REAL abandon API under the daily cap (default 2): two
    abandons at local 23:59:59.999 fill THAT day's quota, the very next
    abandon at local 00:00:00 succeeds because the window rolled, and
    the second one in the new day hits the cap with the typed
    ABANDON_LIMIT_REACHED -- the grouping is the local business day,
    never a UTC day."""
    tz = _tz_from_settings
    last_ms_local = datetime(2026, 9, 30, 23, 59, 59, 999000, tzinfo=tz)
    midnight_local = datetime(2026, 10, 1, 0, 0, 0, tzinfo=tz)
    world = _World(uuid.uuid4().hex[:12])
    async with _stack_at(datetime(2026, 9, 30, 12, 0, tzinfo=UTC)) as stack:
        clock: SteppableClock = stack["clock"]
        teacher_headers, student_headers, task_id = await _seed_one_student_world(
            db_factory, world, assignment_count=6, duration_minutes=4320
        )
        try:
            transport = httpx.ASGITransport(app=stack["app"])
            async with httpx.AsyncClient(
                transport=transport, base_url="http://e2e-boundary"
            ) as client:

                async def _claim_then_abandon(at: datetime) -> dict[str, Any]:
                    """Claim a fresh assignment at the current instant,
                    move the clock to ``at``, abandon through the real
                    API, and return the parsed response."""
                    claim_id = await _claim(client, student_headers, task_id)
                    clock.advance_to(at)
                    response = await client.post(
                        f"/api/v1/claims/{claim_id}/abandon", headers=student_headers
                    )
                    return {
                        "status": response.status_code,
                        "claim_id": claim_id,
                        "body": response.json(),
                    }

                # Local day D (2026-09-30): two abandons fill the cap...
                first = await _claim_then_abandon(last_ms_local.astimezone(UTC))
                assert first["status"] == 200, first["body"]
                assert first["body"]["status"] == ClaimStatus.ABANDONED.value
                second = await _claim_then_abandon(last_ms_local.astimezone(UTC))
                assert second["status"] == 200, second["body"]

                # ...local midnight rolls the window: the next abandon
                # lands in day D+1 and succeeds...
                third = await _claim_then_abandon(midnight_local.astimezone(UTC))
                assert third["status"] == 200, third["body"]

                # ...the second one in D+1 fills its cap again...
                fourth = await _claim_then_abandon(midnight_local.astimezone(UTC) + _MS)
                assert fourth["status"] == 200, fourth["body"]

                # ...and a fifth in the SAME local day is the typed cap.
                fifth = await _claim_then_abandon(
                    midnight_local.astimezone(UTC) + _MS + _MS
                )
                assert fifth["status"] == 409, fifth["body"]
                assert fifth["body"]["error"]["code"] == "ABANDON_LIMIT_REACHED"

            # The persisted attribution instants are the exact stepped
            # ones: the last two sit at/after the local-midnight UTC
            # instant, the first two strictly before it.
            rows = []
            for result in (first, second, third, fourth):
                row = await _load_claim(db_factory, uuid.UUID(result["claim_id"]))
                assert row.status == ClaimStatus.ABANDONED.value
                assert row.terminal_at is not None
                rows.append(row.terminal_at)
            boundary = midnight_local.astimezone(UTC)
            assert all(terminal < boundary for terminal in rows[:2])
            assert all(terminal >= boundary for terminal in rows[2:])
            fifth_row = await _load_claim(db_factory, uuid.UUID(fifth["claim_id"]))
            assert fifth_row.status == ClaimStatus.CLAIMED.value  # untouched
        finally:
            await _teardown_world(db_factory, world)


# --- group 3: DST zone-awareness ------------------------------------------------------


async def test_dst_switch_day_period_grouping_is_zone_aware(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Business timezone = America/New_York (injected through EXISTING
    seams; see the module docstring): the 2026-03-08 spring-forward day
    is a 23-hour local day, and an entry at 16:30Z -- which fixed-+8
    arithmetic would bucket into the NEXT local day -- lands on the
    switch day's own board, both in the REAL projection (injectable
    ``tz``) and in the REAL board route (``get_settings`` override)."""
    ny = ZoneInfo("America/New_York")
    switch_day = date(2026, 3, 8)  # US DST start: 02:00 EST -> 03:00 EDT
    day_start, day_end = day_bounds(switch_day, ny)
    assert day_end - day_start == timedelta(hours=23)  # the folded day

    # 16:30Z on the switch day = 12:30 EDT: inside the 2026-03-08 local
    # day. Under a hardcoded +8 it would be 2026-03-09 00:30 local --
    # the WRONG (next) day, exactly the misbucketing this test forbids.
    instant = datetime(2026, 3, 8, 16, 30, tzinfo=UTC)

    async with _stack_at(datetime(2026, 3, 7, 12, 0, tzinfo=UTC)) as stack:
        app = stack["app"]
        clock: SteppableClock = stack["clock"]
        broker: aioredis.Redis = stack["broker"]
        # API-side seam: every route reads business_timezone through the
        # ``get_settings`` FastAPI dependency -- an existing override
        # point, no production change (see the report).
        ny_settings = get_settings().model_copy(
            update={"business_timezone": "America/New_York"}
        )
        app.dependency_overrides[get_settings] = lambda: ny_settings

        world = _World(uuid.uuid4().hex[:12])
        student = await seed_student(db_factory, run=f"0{world.run}")
        world.user_ids.append(student.user_id)
        member = str(student.user_id)
        expected_keys = {
            daily_key(switch_day),
            monthly_key(date(2026, 3, 1)),
            ALL_TIME_KEY,
        }
        try:
            async with db_factory() as db:
                db.add(
                    PointWallet(
                        user_id=student.user_id,
                        available_points=40,
                        earned_points=40,
                    )
                )
                await db.commit()
            await _seed_ranking_entry(
                db_factory,
                student_id=student.user_id,
                amount=40,
                ranking_effective_at=instant,
            )

            # Worker-side seam: the projection job's ``projection``
            # argument (its production default reads Settings lazily --
            # an existing injectable constructor, no production change).
            job = await asyncio.to_thread(
                run_ranking_projection,
                member,
                instant.isoformat(),
                request_id=f"e2e-boundary-dst-{world.run}",
                projection=RankingRedisProjection(tz=ny),
            )
            assert set(job["updated_keys"]) == expected_keys
            assert await broker.zscore(daily_key(switch_day), member) == 40.0

            clock.advance_to(instant)
            headers = {
                "Authorization": (
                    f"Bearer {await _mint_access_token(db_factory, student.user_id)}"
                )
            }
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://e2e-boundary"
            ) as client:
                daily = await _board(client, headers, "/rankings/daily")
                monthly = await _board(client, headers, "/rankings/monthly")
                assert daily["my_score"] == 40
                assert monthly["my_score"] == 40
        finally:
            app.dependency_overrides.pop(get_settings, None)
            for key in expected_keys:
                with contextlib.suppress(Exception):
                    await broker.zrem(key, member)
            await clean_world(db_factory, user_ids=world.user_ids)


@pytest_asyncio.fixture
async def _tz_from_settings() -> ZoneInfo:
    """The suite's configured business timezone (Asia/Shanghai in the
    e2e environment): the rollover tests derive their boundary instants
    FROM the zone instead of restating a +8 offset, so they stay honest
    if the configured zone ever changes."""
    return ZoneInfo(get_settings().business_timezone)
