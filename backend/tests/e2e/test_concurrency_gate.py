# backend/tests/e2e/test_concurrency_gate.py
"""Concurrency release gate (plan 10 task 5; quality gate G15: the
concurrency invariants need REAL PostgreSQL with independent
connections -- mocks prove nothing).

Integration-matrix form, not a re-derivation of the single-domain
suites: every racer is a real HTTP request through ``create_app()``'s
real routes (or the real worker job core), every transaction lands on
real PostgreSQL through the app's pooled independent sessions, and the
invariants asserted are the release-gate set:

- **50 users race 10 assignments:** exactly ten unique claims, forty
  typed 409 conflicts, and ZERO 5xx (the exception-classification
  rule: every loss is a business envelope, never an internal error).
- **Same-user quota race:** two held claims + two concurrent fresh
  claims -> exactly one new claim (the user-row lock serializes
  COUNT-then-INSERT).
- **Same-task same-user race:** two concurrent claims -> at most one.
- **Concurrent approve:** the task owner and an admin race the §14
  ten-step transaction -> exactly one ASSIGNMENT_REWARD ledger row
  (the UNIQUE source triple + the row lock are the backstops), one
  wallet credit, one confirmation history row.
- **Redemption double-spend and last-stock:** the wallet-row and
  item-row locks leave balances, spendable points, and derived stock
  occupancy never negative / never oversold.
- **submit-vs-expire race:** the pause-race harness (the
  test_submit_expire_race convention): a protecting transaction holds
  the claim-row FOR UPDATE paused before its commit while the REAL
  expiry job tries to release; the in-window submission's protection
  commits first and the re-judged expiry must answer PROTECTED -- the
  real validation pipeline then continues and the student keeps the
  on-time tier.

The five-consecutive-clean-runs acceptance (plan task 5 step 7) is a
property of this whole module: ``for i in 1..5: pytest tests/e2e/
test_concurrency_gate.py -q`` must stay green back to back.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.points.enums import LedgerType, ReservationStatus
from app.modules.points.models import (
    PointReservation,
    PointsLedger,
    PointWallet,
    RewardItem,
    RewardRedemption,
)
from app.modules.submissions.enums import ValidationStatus
from app.modules.submissions.models import RewardLockHistory, Submission
from app.modules.tasks.enums import (
    AssignmentAvailability,
    ClaimStatus,
    RewardLockStatus,
)
from app.modules.tasks.models import (
    ACTIVE_CLAIM_STATUSES,
    Assignment,
    AssignmentClaim,
)
from app.workers.jobs.validate_submission import run_submission_validation
from tests.e2e.clock_control import (
    SteppableClock,
    install_clock_override,
    trigger_expire_claims_scan,
)
from tests.e2e.factories import (
    clean_world,
    seed_admin_confirmed_totp,
    seed_claim,
    seed_points_balance,
    seed_reward_item,
    seed_student,
    seed_task_with_assignments,
    seed_teacher_confirmed_totp,
    snapshot_honor_ids,
)
from tests.e2e.test_deadline_flows import _claim, _submit, _validate
from tests.e2e.test_happy_path import _mint_access_token, _purge_objects

pytestmark = pytest.mark.e2e

_BROKER_QUEUE_KEY = "celery"

#: A 60-minute RELATIVE task (deadline = claim + 1h, grace = +24h): the
#: submit/expire race needs small exact steps from the claim.
_DURATION_MINUTES = 60
_T0 = datetime(2026, 9, 21, 8, 0, tzinfo=UTC)
_MS = timedelta(milliseconds=1)

_GOOD_CSV = (
    b"url,title\n"
    b"https://example.com/note/1,\xe7\xac\xac\xe4\xb8\x80\xe6\x9d\xa1\n"
    b"https://example.com/note/2,\xe7\xac\xac\xe4\xba\x8c\xe6\x9d\xa1\n"
)


class _World:
    """One run's seeded ids (the deadline-flows bookkeeping shape)."""

    def __init__(self, run: str) -> None:
        self.run = run
        self.user_ids: list[UUID] = []
        self.task_ids: list[UUID] = []
        self.reward_item_ids: list[UUID] = []
        self.honors_before: set[UUID] | None = None


@asynccontextmanager
async def _stack_at(start: datetime) -> AsyncIterator[dict[str, Any]]:
    """App under lifespan + stepped clock + broker (the deadline-flows
    stack). Process-wide singleton resets live in the e2e conftest;
    teardown restores the clock override and the broker queue."""
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


@pytest_asyncio.fixture
async def _stack() -> AsyncIterator[dict[str, Any]]:
    async with _stack_at(_T0) as stack:
        yield stack


@dataclass(slots=True)
class _SeededTask:
    """One seeded task the test still needs handles for."""

    fixture: Any  # tests.e2e.factories.TaskFixture
    teacher_headers: dict[str, str]


async def _seed_teacher_and_tasks(
    db_factory: async_sessionmaker[AsyncSession],
    world: _World,
    *,
    assignment_counts: list[int],
) -> tuple[_SeededTask, ...]:
    """One teacher owning ``len(assignment_counts)`` published tasks;
    returns per-task handles (fixture for ORM follow-ups like
    ``seed_claim``, teacher headers for review routes)."""
    teacher = await seed_teacher_confirmed_totp(db_factory, run=world.run)
    world.user_ids.append(teacher.user_id)
    token = await _mint_access_token(db_factory, teacher.user_id)
    teacher_headers = {"Authorization": f"Bearer {token}"}
    seeded: list[_SeededTask] = []
    for index, count in enumerate(assignment_counts):
        task = await seed_task_with_assignments(
            db_factory,
            teacher_id=teacher.user_id,
            run=f"{index:02d}{world.run}",
            assignment_count=count,
            duration_minutes=_DURATION_MINUTES,
        )
        world.task_ids.append(task.task_id)
        seeded.append(_SeededTask(fixture=task, teacher_headers=teacher_headers))
    world.honors_before = await snapshot_honor_ids(db_factory)
    return tuple(seeded)


async def _seed_students(
    db_factory: async_sessionmaker[AsyncSession],
    world: _World,
    count: int,
) -> list[tuple[UUID, dict[str, str]]]:
    """``count`` ACTIVE students as (user id, bearer headers) pairs;
    two-digit decimal run prefixes keep usernames and phones
    collision-free (the factories' hex-folding recipe needs a
    fixed-width prefix)."""
    seeded = []
    for index in range(count):
        student = await seed_student(db_factory, run=f"{index:02d}{world.run}")
        world.user_ids.append(student.user_id)
        token = await _mint_access_token(db_factory, student.user_id)
        seeded.append((student.user_id, {"Authorization": f"Bearer {token}"}))
    return seeded


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
        reward_item_ids=world.reward_item_ids,
        honor_ids_before=world.honors_before,
    )


# --- the racing harness ------------------------------------------------------------


@dataclass(slots=True)
class ApiCall:
    """One raced request's terminal state: HTTP status, the business
    envelope's stable code (present on every typed failure), and the
    parsed body."""

    status: int
    code: str | None
    body: dict[str, Any]


async def _post_on_barrier(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    start: asyncio.Event,
) -> ApiCall:
    """Park on the barrier, then fire one POST and reduce it to an
    ``ApiCall`` (a non-JSON body would itself be a release-gate
    failure -- the envelope contract -- so it degrades to an empty
    dict and the status/code asserts catch it)."""
    await start.wait()
    response = await client.post(url, headers=headers)
    try:
        body = response.json()
    except ValueError:
        body = {}
    error = body.get("error") or {}
    return ApiCall(status=response.status_code, code=error.get("code"), body=body)


async def _race_posts(
    calls: list[Callable[[asyncio.Event], Awaitable[ApiCall]]],
) -> list[ApiCall]:
    """Release every racer through one barrier so the transactions
    genuinely contend (the integration suite's Event-barrier
    convention; the app's engine pool bounds how many overlap at once
    -- row-level contention, not connection count, is what these
    tests exercise)."""
    start = asyncio.Event()
    tasks = [asyncio.create_task(call(start)) for call in calls]
    await asyncio.sleep(0.05)  # park every racer on the barrier
    start.set()
    return list(await asyncio.gather(*tasks))


async def _await_row_lock_waiter(
    db_factory: async_sessionmaker[AsyncSession],
    *,
    count: int = 1,
    timeout: float = 5.0,
) -> None:
    """Poll until ``count`` distinct backends hold an UNGRANTED lock
    request in this database -- deterministic proof a racer PARKED on
    the row lock, never that it is merely slow (the integration
    suite's pg_locks predicate; see its docstring for why query/state
    columns are deliberately not used)."""
    deadline = time.monotonic() + timeout
    async with db_factory() as session:
        while True:
            blocked = await session.scalar(
                text(
                    "SELECT count(DISTINCT locks.pid) "
                    "FROM pg_locks locks "
                    "JOIN pg_stat_activity activity ON activity.pid = locks.pid "
                    "WHERE locks.granted = false "
                    "AND activity.datname = current_database() "
                    "AND locks.pid <> pg_backend_pid()"
                )
            )
            if (blocked or 0) >= count:
                return
            if time.monotonic() > deadline:
                pytest.fail(
                    f"expected {count} row-lock waiter(s), saw {blocked} "
                    f"within {timeout}s"
                )
            await asyncio.sleep(0.05)


def _split(results: list[ApiCall]) -> tuple[list[ApiCall], list[ApiCall]]:
    """Winners (201) vs everyone else -- every non-201 must be a typed
    conflict, checked per gate."""
    successes = [result for result in results if result.status == 201]
    conflicts = [result for result in results if result.status != 201]
    return successes, conflicts


def _assert_no_server_errors(results: list[ApiCall]) -> None:
    """The zero-500 classification rule, stated up front in every gate:
    no answer may reach the 5xx family."""
    offenders = [
        (result.status, result.code) for result in results if result.status >= 500
    ]
    assert not offenders, offenders


# --- gate 1: 50 users race 10 assignments ------------------------------------------


async def test_fifty_students_race_ten_assignments(
    db_factory: async_sessionmaker[AsyncSession], _stack: dict[str, Any]
) -> None:
    """The headline allocation invariant at the API level: exactly the
    ten assignments are handed out once each, everyone else gets the
    typed NO_ASSIGNMENT_AVAILABLE 409, and nothing anywhere answers
    5xx."""
    world = _World(uuid.uuid4().hex[:12])
    (task,) = await _seed_teacher_and_tasks(db_factory, world, assignment_counts=[10])
    student_pairs = await _seed_students(db_factory, world, 50)
    url = f"/api/v1/tasks/{task.fixture.task_id}/claim"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_stack["app"]),
            base_url="http://e2e-gate",
        ) as client:
            results = await _race_posts(
                [
                    functools.partial(_post_on_barrier, client, url, headers)
                    for _, headers in student_pairs
                ]
            )

        _assert_no_server_errors(results)
        successes, conflicts = _split(results)
        assert len(successes) == 10
        claim_ids = {result.body["claim_id"] for result in successes}
        assert len(claim_ids) == 10

        assert len(conflicts) == 40
        assert {result.code for result in conflicts} == {"NO_ASSIGNMENT_AVAILABLE"}
        assert all(result.status == 409 for result in conflicts)

        task_uuid = task.fixture.task_id
        async with db_factory() as db:
            claims = (
                (
                    await db.execute(
                        select(AssignmentClaim).where(
                            AssignmentClaim.task_id == task_uuid,
                            AssignmentClaim.status.in_(ACTIVE_CLAIM_STATUSES),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(claims) == 10
            assert all(claim.status == ClaimStatus.CLAIMED.value for claim in claims)
            assert {str(claim.id) for claim in claims} == claim_ids
            occupied = set(
                (
                    await db.execute(
                        select(Assignment.id).where(
                            Assignment.task_id == task_uuid,
                            Assignment.availability_status
                            == AssignmentAvailability.OCCUPIED.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
            available = (
                (
                    await db.execute(
                        select(Assignment.id).where(
                            Assignment.task_id == task_uuid,
                            Assignment.availability_status
                            == AssignmentAvailability.AVAILABLE.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(occupied) == 10
        assert available == []
        assert {claim.assignment_id for claim in claims} == occupied
    finally:
        await _teardown_world(db_factory, world)


# --- gate 2: the same-user quota race -----------------------------------------------


async def test_quota_race_two_fresh_claims_land_exactly_one(
    db_factory: async_sessionmaker[AsyncSession], _stack: dict[str, Any]
) -> None:
    """A student already holding two CLAIMED claims (quota 3) fires two
    concurrent claims on two fresh tasks: the user-row lock
    serializes the count, exactly one lands, the loser gets the typed
    ASSIGNMENT_LIMIT_REACHED."""
    world = _World(uuid.uuid4().hex[:12])
    held_a, held_b, fresh_a, fresh_b = await _seed_teacher_and_tasks(
        db_factory, world, assignment_counts=[1, 1, 1, 1]
    )
    ((student_id, student_headers),) = await _seed_students(db_factory, world, 1)
    for held in (held_a, held_b):
        await seed_claim(db_factory, task=held.fixture, student_id=student_id)
    urls = [
        f"/api/v1/tasks/{fresh.fixture.task_id}/claim" for fresh in (fresh_a, fresh_b)
    ]
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_stack["app"]),
            base_url="http://e2e-gate",
        ) as client:
            results = await _race_posts(
                [
                    functools.partial(_post_on_barrier, client, url, student_headers)
                    for url in urls
                ]
            )

        _assert_no_server_errors(results)
        successes, conflicts = _split(results)
        assert len(successes) == 1
        assert len(conflicts) == 1
        assert conflicts[0].status == 409
        assert conflicts[0].code == "ASSIGNMENT_LIMIT_REACHED"

        async with db_factory() as db:
            active = (
                (
                    await db.execute(
                        select(AssignmentClaim.id).where(
                            AssignmentClaim.user_id == student_id,
                            AssignmentClaim.status.in_(ACTIVE_CLAIM_STATUSES),
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(active) == 3  # exactly at the quota, never past it
    finally:
        await _teardown_world(db_factory, world)


# --- gate 3: same task, same user --------------------------------------------------


async def test_same_task_same_user_race_claims_at_most_once(
    db_factory: async_sessionmaker[AsyncSession], _stack: dict[str, Any]
) -> None:
    """Two simultaneous claims by one user on one task: the user-level
    serialization lets exactly one through; the other observes the
    non-terminal claim and gets the typed TASK_ACTIVE_CLAIM_EXISTS."""
    world = _World(uuid.uuid4().hex[:12])
    (task,) = await _seed_teacher_and_tasks(db_factory, world, assignment_counts=[2])
    ((student_id, student_headers),) = await _seed_students(db_factory, world, 1)
    url = f"/api/v1/tasks/{task.fixture.task_id}/claim"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_stack["app"]),
            base_url="http://e2e-gate",
        ) as client:
            results = await _race_posts(
                [
                    functools.partial(_post_on_barrier, client, url, student_headers)
                    for _ in range(2)
                ]
            )

        _assert_no_server_errors(results)
        successes, conflicts = _split(results)
        assert len(successes) == 1
        assert len(conflicts) == 1
        assert conflicts[0].status == 409
        assert conflicts[0].code == "TASK_ACTIVE_CLAIM_EXISTS"

        async with db_factory() as db:
            active = (
                (
                    await db.execute(
                        select(AssignmentClaim).where(
                            AssignmentClaim.task_id == task.fixture.task_id,
                            AssignmentClaim.user_id == student_id,
                            AssignmentClaim.status.in_(ACTIVE_CLAIM_STATUSES),
                        )
                    )
                )
                .scalars()
                .all()
            )
            occupied = (
                (
                    await db.execute(
                        select(Assignment.id).where(
                            Assignment.task_id == task.fixture.task_id,
                            Assignment.availability_status
                            == AssignmentAvailability.OCCUPIED.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(active) == 1
        assert len(occupied) == 1
    finally:
        await _teardown_world(db_factory, world)


# --- gate 4: concurrent approve ----------------------------------------------------


async def test_concurrent_reviewers_approve_grant_exactly_once(
    db_factory: async_sessionmaker[AsyncSession], _stack: dict[str, Any]
) -> None:
    """The owner teacher and an admin race the §14 approve transaction
    on one machine-validated submission: exactly one grants -- one
    ASSIGNMENT_REWARD ledger row (the UNIQUE source triple), one
    wallet credit, one confirmation history row -- and the loser
    returns the idempotent already-reviewed answer. Never a 5xx."""
    world = _World(uuid.uuid4().hex[:12])
    (task,) = await _seed_teacher_and_tasks(db_factory, world, assignment_counts=[1])
    # The confirmed-TOTP row is the management guard's precondition
    # (spec §33.4): the staff review surface refuses a credential-less
    # admin with TOTP_SETUP_REQUIRED before any race can start.
    admin = await seed_admin_confirmed_totp(db_factory, run=f"00{world.run}")
    world.user_ids.append(admin.user_id)
    admin_headers = {
        "Authorization": f"Bearer {await _mint_access_token(db_factory, admin.user_id)}"
    }
    ((student_id, student_headers),) = await _seed_students(db_factory, world, 1)
    clock: SteppableClock = _stack["clock"]
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_stack["app"]),
            base_url="http://e2e-gate",
        ) as client:
            claim_id = await _claim(client, student_headers, str(task.fixture.task_id))
            clock.advance(timedelta(minutes=30))  # on-time submit window
            submission_id = await _submit(
                client, student_headers, claim_id, _GOOD_CSV, filename="并发审批.csv"
            )
            await _validate(submission_id, world.run)

            url = f"/api/v1/teacher/submissions/{submission_id}/approve"
            results = await _race_posts(
                [
                    functools.partial(
                        _post_on_barrier, client, url, task.teacher_headers
                    ),
                    functools.partial(_post_on_barrier, client, url, admin_headers),
                ]
            )

        _assert_no_server_errors(results)
        reviewed = sorted(result.body["already_reviewed"] for result in results)
        assert reviewed == [False, True]
        winner = next(
            result for result in results if result.body["already_reviewed"] is False
        )
        assert winner.body["points_granted"] == 100
        assert winner.body["claim_status"] == ClaimStatus.COMPLETED.value
        assert winner.body["reward_lock_status"] == RewardLockStatus.CONFIRMED.value

        claim_uuid = uuid.UUID(claim_id)
        async with db_factory() as db:
            reward_entries = (
                (
                    await db.execute(
                        select(PointsLedger).where(
                            PointsLedger.user_id == student_id,
                            PointsLedger.ledger_type == LedgerType.ASSIGNMENT_REWARD,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(reward_entries) == 1  # the UNIQUE triple's one row
            assert reward_entries[0].amount == 100
            assert reward_entries[0].source_id == claim_uuid
            wallet = await db.get(PointWallet, student_id)
            assert wallet is not None
            assert wallet.available_points == 100  # credited exactly once
            assert wallet.earned_points == 100
            confirmations = (
                (
                    await db.execute(
                        select(RewardLockHistory).where(
                            RewardLockHistory.claim_id == claim_uuid,
                            RewardLockHistory.lock_status_to
                            == RewardLockStatus.CONFIRMED.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(confirmations) == 1
            claim = await db.get(AssignmentClaim, claim_uuid)
            assert claim is not None
            assert claim.status == ClaimStatus.COMPLETED.value
            assignment = await db.get(Assignment, claim.assignment_id)
            assert assignment is not None
            assert (
                assignment.availability_status == AssignmentAvailability.COMPLETED.value
            )
    finally:
        await _teardown_world(db_factory, world)


# --- gate 5: redemption double-spend and last stock --------------------------------


async def _wallet_view(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> dict[str, Any]:
    response = await client.get("/api/v1/points/me", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_concurrent_double_spend_freezes_at_most_one(
    db_factory: async_sessionmaker[AsyncSession], _stack: dict[str, Any]
) -> None:
    """150 points, two concurrent 100-point redemptions of the same
    item: exactly one freezes (the wallet-row lock serializes
    same-user requests), the loser gets the typed
    INSUFFICIENT_POINTS, and neither the wallet nor the spendable
    figure ever goes negative."""
    world = _World(uuid.uuid4().hex[:12])
    ((student_id, student_headers),) = await _seed_students(db_factory, world, 1)
    await seed_points_balance(
        db_factory, student_id=student_id, amount=150, source_id=uuid.uuid4()
    )
    item = await seed_reward_item(db_factory, run=world.run, point_cost=100)
    world.reward_item_ids.append(item.reward_item_id)
    url = f"/api/v1/rewards/{item.reward_item_id}/redeem"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_stack["app"]),
            base_url="http://e2e-gate",
        ) as client:
            results = await _race_posts(
                [
                    functools.partial(_post_on_barrier, client, url, student_headers)
                    for _ in range(2)
                ]
            )

            _assert_no_server_errors(results)
            successes, conflicts = _split(results)
            assert len(successes) == 1
            assert len(conflicts) == 1
            assert conflicts[0].status == 409
            assert conflicts[0].code == "INSUFFICIENT_POINTS"

            wallet = await _wallet_view(client, student_headers)
            assert wallet["available_points"] == 150  # a freeze, not a spend
            assert wallet["spendable_points"] == 50
            assert wallet["available_points"] >= 0  # never -50
            assert wallet["spendable_points"] >= 0

        async with db_factory() as db:
            redemptions = (
                (
                    await db.execute(
                        select(RewardRedemption).where(
                            RewardRedemption.user_id == student_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(redemptions) == 1
            assert redemptions[0].status == "REQUESTED"
            reservations = (
                (
                    await db.execute(
                        select(PointReservation).where(
                            PointReservation.user_id == student_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(reservations) == 1  # at most one active freeze
            assert reservations[0].status == ReservationStatus.ACTIVE.value
            assert reservations[0].points == 100
    finally:
        await _teardown_world(db_factory, world)


async def test_last_stock_race_never_oversells(
    db_factory: async_sessionmaker[AsyncSession], _stack: dict[str, Any]
) -> None:
    """stock=1, two funded students race the same item: exactly one
    unit is taken (derived occupancy == 1), the loser gets the typed
    REWARD_OUT_OF_STOCK, and both wallets stay whole."""
    world = _World(uuid.uuid4().hex[:12])
    student_pairs = await _seed_students(db_factory, world, 2)
    for student_id, _headers in student_pairs:
        await seed_points_balance(
            db_factory, student_id=student_id, amount=100, source_id=uuid.uuid4()
        )
    item = await seed_reward_item(db_factory, run=world.run, point_cost=50)
    world.reward_item_ids.append(item.reward_item_id)
    async with db_factory() as db:
        row = await db.get(RewardItem, item.reward_item_id)
        assert row is not None
        row.stock = 1  # the last unit
        await db.commit()
    url = f"/api/v1/rewards/{item.reward_item_id}/redeem"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_stack["app"]),
            base_url="http://e2e-gate",
        ) as client:
            results = await _race_posts(
                [
                    functools.partial(_post_on_barrier, client, url, headers)
                    for _, headers in student_pairs
                ]
            )

            _assert_no_server_errors(results)
            successes, conflicts = _split(results)
            assert len(successes) == 1
            assert len(conflicts) == 1
            assert conflicts[0].status == 409
            assert conflicts[0].code == "REWARD_OUT_OF_STOCK"

            wallets = [
                await _wallet_view(client, headers) for _, headers in student_pairs
            ]
            assert all(wallet["available_points"] == 100 for wallet in wallets)
            assert all(wallet["spendable_points"] >= 0 for wallet in wallets)
            spendables = sorted(wallet["spendable_points"] for wallet in wallets)
            assert spendables == [50, 100]  # the winner froze, the loser did not

        async with db_factory() as db:
            occupying = (
                (
                    await db.execute(
                        select(RewardRedemption).where(
                            RewardRedemption.reward_item_id == item.reward_item_id,
                            RewardRedemption.status.in_(
                                ("REQUESTED", "UNDER_REVIEW", "APPROVED", "FULFILLED")
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(occupying) == 1  # exactly one unit taken, never two
    finally:
        await _teardown_world(db_factory, world)


# --- gate 6: the submit-vs-expire race ---------------------------------------------


async def test_in_window_submission_beats_concurrent_expiry(
    db_factory: async_sessionmaker[AsyncSession], _stack: dict[str, Any]
) -> None:
    """The pause-race shape (the integration suite's forced-overlap
    convention), at the release-gate level: the protecting transaction
    -- standing in for the validation pipeline's claim-lock phase,
    paused BEFORE its commit -- holds the claim-row FOR UPDATE while
    the REAL expiry job (real inspector, real event composition) tries
    to release the past-grace claim. The protection commits first; the
    re-judged expiry must answer PROTECTED and release nothing; the
    REAL validation job then continues and the student keeps the
    on-time tier the race tried to take away."""
    world = _World(uuid.uuid4().hex[:12])
    (task,) = await _seed_teacher_and_tasks(db_factory, world, assignment_counts=[1])
    ((student_id, student_headers),) = await _seed_students(db_factory, world, 1)
    clock: SteppableClock = _stack["clock"]
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_stack["app"]),
            base_url="http://e2e-gate",
        ) as client:
            claim_id = await _claim(client, student_headers, str(task.fixture.task_id))
            clock.advance(timedelta(minutes=30))  # in-window submit instant
            submission_id = await _submit(
                client, student_headers, claim_id, _GOOD_CSV, filename="竞态.csv"
            )

        claim_uuid = uuid.UUID(claim_id)
        async with db_factory() as db:
            claim = await db.get(AssignmentClaim, claim_uuid)
            assert claim is not None
            grace_deadline = claim.grace_deadline_at
            assert grace_deadline is not None
            assignment_id = claim.assignment_id

        # The protecting transaction: claim-row FOR UPDATE, the real
        # pipeline's VALIDATING write applied, paused before commit.
        async with db_factory() as protector:
            protected = await protector.scalar(
                select(AssignmentClaim)
                .where(AssignmentClaim.id == claim_uuid)
                .with_for_update()
            )
            assert protected is not None
            protected.status = ClaimStatus.VALIDATING.value
            await protector.flush()

            # The REAL expiry job at an explicitly past-grace instant
            # (the queue never owns deadline judgements): discovery
            # reads the still-CLAIMED row, then the per-id transaction
            # PARKS on the protecting lock.
            expiry_task = asyncio.create_task(
                trigger_expire_claims_scan(grace_deadline + _MS)
            )
            await _await_row_lock_waiter(db_factory)

            await protector.commit()  # the protection lands

        payloads = await expiry_task
        outcomes = {
            uuid.UUID(entry["claim_id"]): entry["outcome"] for entry in payloads
        }
        assert outcomes.get(claim_uuid) == "PROTECTED"

        # The real pipeline continues over the protected claim: the
        # stale-VALIDATING rerun clause accepts it, the machine gate
        # validates the in-window file, and the reward locks at the
        # on-time tier -- exactly what the raced expiry would have
        # destroyed.
        await asyncio.to_thread(
            run_submission_validation,
            submission_id,
            request_id=f"e2e-gate-race-{world.run}",
        )

        async with db_factory() as db:
            final = await db.get(AssignmentClaim, claim_uuid)
            assert final is not None
            assert final.status == ClaimStatus.UNDER_REVIEW.value
            assert final.terminal_at is None  # never EXPIRED
            assert final.reward_lock_status == RewardLockStatus.PROVISIONAL.value
            assert final.reward_tier_locked == 100  # the on-time tier survived
            assert final.locked_reward_points == 100
            submission = await db.get(Submission, uuid.UUID(submission_id))
            assert submission is not None
            assert submission.validation_status == ValidationStatus.VALIDATED.value
            assignment = await db.get(Assignment, assignment_id)
            assert assignment is not None
            assert (
                assignment.availability_status
                == AssignmentAvailability.OCCUPIED.value  # never released
            )
    finally:
        await _teardown_world(db_factory, world)
