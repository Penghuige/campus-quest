# backend/tests/integration/points/test_submission_reward.py
"""Submission approval wired to the REAL assignment reward (spec §14;
plan 05 task 3).

Plan 04 proved the §14 approve transaction against the in-memory
``FakePointsRewardPort``; these tests swap in the real
``PointsRewardPortAdapter`` over ``LedgerService`` and prove the wiring
end to end against real PostgreSQL — no fake anywhere on the grant path:

- **Composition-root binding:** the submissions router's
  ``get_points_port`` provider constructs the points-module adapter over
  the REQUEST's session, so the §14 step-8 grant joins the approve
  transaction and the caller's commit decides its fate (the adapter
  never commits — the task-2 verified contract).
- **The two-reviewer race (§14):** two AUTHORIZED reviewers approve the
  same Submission concurrently on independent sessions released on one
  barrier; the claim row lock serializes them — the claim completes
  exactly once, ONE ASSIGNMENT_REWARD ledger row lands (the port's
  effective call count), the wallet increments once (available AND
  earned), and the loser reads the terminal claim under the lock and
  answers the idempotent ALREADY_REVIEWED with nothing written.
- **End-to-end mapping:** one approve maps the frozen port call to the
  UNIQUE source triple ('ASSIGNMENT_CLAIM', claim_id,
  'ASSIGNMENT_REWARD') — the idempotency-key mechanism — with the
  LOCKED points (80, not the base 100 snapshot) as the amount and the
  claim's lock time as the ranking attribution; the wallet projection
  lands at the locked points; a sequential replay approve answers
  ALREADY_REVIEWED and leaves exactly one entry and one increment; the
  SUBMISSION_APPROVED audit event payload is byte-for-byte the frozen
  shape (the fake-to-real swap leaks nothing into the audit stream).

Harness notes (backend-engineering §7; the two integration siblings'
conventions): async tests on the shared ``db_engine`` fixture with a
per-test ``async_sessionmaker`` — the concurrency scenario needs
independent sessions with REAL commits (the savepoint-wrapped
``db_session`` fixture is invisible to other connections); connections
are warmed before the barrier so asyncpg setup cannot hide the
interleaving; every test removes its rows with explicit committed
DELETEs in FK order in ``finally`` (review rows -> lock history ->
submissions -> collaborators -> claims -> assignments -> tasks ->
ledger -> wallet -> users); usernames embed a per-run token so rows
leaked by an aborted run cannot collide with a later seeding pass.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor, InMemoryEventCollector
from app.modules.identity.models import User
from app.modules.points.enums import LedgerType
from app.modules.points.ledger_service import LedgerService, PointsRewardPortAdapter
from app.modules.points.models import PointsLedger, PointWallet
from app.modules.rankings.redis_projection import RankingRedisProjection
from app.modules.submissions.enums import ValidationStatus
from app.modules.submissions.models import (
    RewardLockHistory,
    Submission,
    SubmissionReview,
)
from app.modules.submissions.review_service import (
    SUBMISSION_APPROVED_EVENT,
    ApprovalResult,
    PointsRewardPort,
    ReviewService,
)
from app.modules.submissions.router import get_points_port
from app.modules.tasks.claim_service import REWARD_POLICY_SNAPSHOT_V1
from app.modules.tasks.enums import (
    AssignmentAvailability,
    ClaimStatus,
    DeadlineMode,
    RewardLockStatus,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.models import (
    Assignment,
    AssignmentClaim,
    Task,
    TaskCollaborator,
)

# Direct-insert password stub (argon2 hash of an unguessable test secret);
# the registration service is deliberately not exercised here.
_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)

# The submit instant the PROVISIONAL lock froze (§17.2 ranking period
# attribution) and the approve instant.
_LOCK_TIME = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
_DEADLINE = _LOCK_TIME + timedelta(hours=1)
_GRACE = _DEADLINE + timedelta(hours=24)
_NOW = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)

# The locked fraction vs the base snapshot: the grant basis must be 80.
_BASE_POINTS = 100
_LOCKED_POINTS = 80
_LOCKED_TIER = 80


# --- row helpers --------------------------------------------------------------------


def _user(run: str, prefix: str, role: Role) -> User:
    return User(
        username=f"{prefix}{run}",
        password_hash=_PASSWORD_HASH,
        nickname=f"{prefix}师{run[-4:]}"
        if role is not Role.STUDENT
        else f"同学{run[-4:]}",
        phone_e164=None,
        role=role,
        status=UserStatus.ACTIVE,
    )


def _task(owner: User) -> Task:
    return Task(
        owner_teacher_id=owner.id,
        title="图书馆座位使用情况采集",
        description="采集各楼层座位占用数据。",
        task_type=TaskType.DATA_CRAWL,
        rarity=TaskRarity.NORMAL,
        base_reward_points=_BASE_POINTS,
        status=TaskStatus.PUBLISHED,
        deadline_mode=DeadlineMode.RELATIVE,
        duration_minutes=4320,
        submission_schema={"columns": [{"name": "seat", "type": "string"}]},
        submission_schema_version=2,
        allowed_file_types=["CSV"],
        max_file_size_bytes=200 * 1024 * 1024,
        notification_channels=["SMS"],
    )


async def _seed(factory: async_sessionmaker[AsyncSession], run: str) -> dict[str, Any]:
    """One reviewable world: owner + student + a REVIEW_SUBMISSIONS
    collaborator (the second authorized reviewer), a PUBLISHED task, an
    OCCUPIED assignment, an UNDER_REVIEW claim with a PROVISIONAL lock
    at the locked fraction, and the current VALIDATED submission."""
    async with factory() as session:
        owner = _user(run, "t", Role.TEACHER)
        student = _user(run, "2025s", Role.STUDENT)
        reviewer = _user(run, "tr", Role.TEACHER)
        session.add_all((owner, student, reviewer))
        await session.flush()
        task = _task(owner)
        session.add(task)
        await session.flush()
        session.add(
            TaskCollaborator(
                task_id=task.id,
                teacher_id=reviewer.id,
                permissions=["REVIEW_SUBMISSIONS"],
            )
        )
        assignment = Assignment(
            task_id=task.id,
            platform="xiaohongshu",
            keyword=f"图书馆{run[-4:]}",
            availability_status=AssignmentAvailability.OCCUPIED.value,
        )
        session.add(assignment)
        await session.flush()
        claim = AssignmentClaim(
            assignment_id=assignment.id,
            task_id=task.id,
            user_id=student.id,
            status=ClaimStatus.UNDER_REVIEW.value,
            claimed_at=_LOCK_TIME - timedelta(days=3),
            deadline_at=_DEADLINE,
            grace_deadline_at=_GRACE,
            reward_policy_snapshot=dict(REWARD_POLICY_SNAPSHOT_V1),
            base_reward_points_snapshot=_BASE_POINTS,
            submission_schema_version=2,
            reward_lock_status=RewardLockStatus.PROVISIONAL.value,
            reward_tier_locked=_LOCKED_TIER,
            locked_reward_points=_LOCKED_POINTS,
            reward_locked_at=_LOCK_TIME,
        )
        session.add(claim)
        await session.flush()
        submission = Submission(
            claim_id=claim.id,
            version=1,
            object_key=f"submissions/{claim.id}/{uuid4()}",
            original_filename="座位.csv",
            declared_type="CSV",
            file_size=128,
            submitted_at=_LOCK_TIME,
            validation_status=ValidationStatus.VALIDATED.value,
            review_status="PENDING_REVIEW",
            retention_until=_LOCK_TIME + timedelta(days=180),
        )
        session.add(submission)
        await session.flush()
        claim.latest_submission_id = submission.id
        await session.commit()
        return {
            "owner": owner,
            "student": student,
            "reviewer": reviewer,
            "task": task,
            "claim": claim,
            "submission": submission,
        }


async def _committed_cleanup(
    factory: async_sessionmaker[AsyncSession],
    *,
    user_ids: list[UUID],
    task_ids: list[UUID],
) -> None:
    """Explicit committed cleanup in FK order; nothing rolls these rows
    back for us (the seeding used real commits)."""
    async with factory() as session:
        if task_ids:
            claim_ids = select(AssignmentClaim.id).where(
                AssignmentClaim.task_id.in_(task_ids)
            )
            submission_ids = select(Submission.id).where(
                Submission.claim_id.in_(claim_ids)
            )
            await session.execute(
                delete(SubmissionReview).where(
                    SubmissionReview.submission_id.in_(submission_ids)
                )
            )
            await session.execute(
                delete(RewardLockHistory).where(
                    RewardLockHistory.claim_id.in_(claim_ids)
                )
            )
            await session.execute(
                delete(Submission).where(Submission.claim_id.in_(claim_ids))
            )
            await session.execute(
                delete(TaskCollaborator).where(TaskCollaborator.task_id.in_(task_ids))
            )
            await session.execute(
                delete(AssignmentClaim).where(AssignmentClaim.task_id.in_(task_ids))
            )
            await session.execute(
                delete(Assignment).where(Assignment.task_id.in_(task_ids))
            )
            await session.execute(delete(Task).where(Task.id.in_(task_ids)))
        for user_id in user_ids:
            await session.execute(
                delete(PointsLedger).where(PointsLedger.user_id == user_id)
            )
            await session.execute(
                delete(PointWallet).where(PointWallet.user_id == user_id)
            )
        if user_ids:
            await session.execute(delete(User).where(User.id.in_(user_ids)))
        await session.commit()


def _service(session: AsyncSession, collector: InMemoryEventCollector) -> ReviewService:
    """The composition the router wires per request: the REAL adapter
    over this caller's session, so the grant joins the approve
    transaction (§14 step 8; the adapter never commits)."""
    return ReviewService(
        clock=FrozenClock(_NOW),
        events=collector,
        points=PointsRewardPortAdapter(ledger=LedgerService(), db=session),
    )


async def _approve(
    factory: async_sessionmaker[AsyncSession],
    collector: InMemoryEventCollector,
    actor: Actor,
    submission_id: UUID,
    *,
    start: asyncio.Event | None = None,
) -> ApprovalResult:
    """One approve on its own session with its own request-scoped
    adapter; optionally parked on the shared barrier so concurrent
    callers genuinely contend for the claim row."""
    async with factory() as session:
        # Warm the pooled connection BEFORE the barrier: asyncpg
        # connection setup otherwise dwarfs the critical section and
        # hides the interleaving under test.
        await session.execute(text("SELECT 1"))
        if start is not None:
            await start.wait()
            await asyncio.sleep(random.uniform(0, 0.005))
        service = _service(session, collector)
        return await service.approve_submission(session, actor, submission_id)


async def _reward_rows(
    factory: async_sessionmaker[AsyncSession], claim_id: UUID
) -> list[PointsLedger]:
    """The claim's ASSIGNMENT_REWARD ledger rows — the port's effective
    call count made visible (exactly one row == exactly one grant)."""
    async with factory() as session:
        rows = await session.scalars(
            select(PointsLedger).where(
                PointsLedger.source_type == "ASSIGNMENT_CLAIM",
                PointsLedger.source_id == claim_id,
                PointsLedger.ledger_type == LedgerType.ASSIGNMENT_REWARD.value,
            )
        )
        return list(rows.all())


# --- composition-root binding --------------------------------------------------------


@pytest.mark.integration
async def test_router_points_provider_binds_real_adapter_over_request_session(
    db_session: AsyncSession,
) -> None:
    """The composition root: ``get_points_port`` constructs the
    points-module ``PointsRewardPortAdapter`` over the session it is
    handed (the request's), never a stub and never a raise — the §14
    step-8 grant joins the approve transaction through THIS binding."""
    port = get_points_port(db_session)
    assert isinstance(port, PointsRewardPortAdapter)
    assert isinstance(port, PointsRewardPort)  # runtime-checkable conformance


# --- the §14 two-reviewer race -------------------------------------------------------


@pytest.mark.integration
async def test_concurrent_reviewers_grant_reward_exactly_once(
    db_engine: AsyncEngine,
) -> None:
    """§14's race with the REAL adapter: the task owner and a
    REVIEW_SUBMISSIONS collaborator approve the same Submission
    concurrently (independent sessions, one barrier). The claim row lock
    serializes them: the claim completes exactly once, ONE
    ASSIGNMENT_REWARD ledger row lands, the wallet increments once
    (available AND earned), and the loser gets the idempotent
    ALREADY_REVIEWED with no grant, no second history row, no second
    event."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    run = uuid4().hex[:8]
    user_ids: list[UUID] = []
    task_ids: list[UUID] = []
    try:
        world = await _seed(factory, run)
        owner = world["owner"]
        student = world["student"]
        reviewer = world["reviewer"]
        claim = world["claim"]
        submission = world["submission"]
        user_ids.extend(user.id for user in (owner, student, reviewer))
        task_ids.append(world["task"].id)
        collector = InMemoryEventCollector()

        # Park both callers on one barrier, then release them together
        # so the transactions genuinely contend for the claim row (the
        # ledger-suite barrier shape).
        start = asyncio.Event()
        tasks = [
            asyncio.create_task(
                _approve(
                    factory,
                    collector,
                    Actor(user_id=owner.id, role=Role(owner.role)),
                    submission.id,
                    start=start,
                )
            ),
            asyncio.create_task(
                _approve(
                    factory,
                    collector,
                    Actor(user_id=reviewer.id, role=Role(reviewer.role)),
                    submission.id,
                    start=start,
                )
            ),
        ]
        await asyncio.sleep(0.05)  # let both park on the barrier
        start.set()
        first, second = await asyncio.gather(*tasks)

        # gather answers in argument order, not arrival order: exactly
        # one side granted, the other got the idempotent result.
        results = (first, second)
        assert sorted(result.already_reviewed for result in results) == [False, True]
        for result in results:
            if result.already_reviewed:
                assert result.grant is None
            else:
                assert result.grant is not None
                assert result.grant.points_granted == _LOCKED_POINTS

        # The effective (ledger) call count: exactly one reward row.
        entries = await _reward_rows(factory, claim.id)
        assert len(entries) == 1

        async with factory() as check:
            claim_row = await check.get(AssignmentClaim, claim.id)
            assert claim_row is not None
            # COMPLETED exactly once: terminal, confirmed lock, one
            # confirmation history row, one APPROVE review row.
            assert claim_row.status == ClaimStatus.COMPLETED.value
            assert claim_row.terminal_at == _NOW
            assert claim_row.reward_lock_status == RewardLockStatus.CONFIRMED.value
            assert claim_row.locked_reward_points == _LOCKED_POINTS
            history = (
                await check.scalars(
                    select(RewardLockHistory).where(
                        RewardLockHistory.claim_id == claim.id
                    )
                )
            ).all()
            assert len(history) == 1
            assert history[0].lock_status_to == RewardLockStatus.CONFIRMED.value
            reviews = (
                await check.scalars(
                    select(SubmissionReview).where(
                        SubmissionReview.submission_id == submission.id
                    )
                )
            ).all()
            assert len(reviews) == 1
            # The wallet incremented ONCE: available + earned.
            wallet = await check.get(PointWallet, student.id)
            assert wallet is not None
            assert wallet.available_points == _LOCKED_POINTS
            assert wallet.earned_points == _LOCKED_POINTS
            # One audit event, never two.
            assert len(collector.of_type(SUBMISSION_APPROVED_EVENT)) == 1
    finally:
        await _committed_cleanup(factory, user_ids=user_ids, task_ids=task_ids)


# --- end-to-end mapping: port call -> ledger row -> wallet projection -----------------


@pytest.mark.integration
async def test_approve_maps_port_call_to_ledger_source_triple_and_projects_wallet(
    db_engine: AsyncEngine,
) -> None:
    """One approve through the REAL adapter: the frozen port call lands
    as the UNIQUE source triple ('ASSIGNMENT_CLAIM', claim_id,
    'ASSIGNMENT_REWARD') — the idempotency-key mechanism — carrying the
    LOCKED points (80, not the 100 base snapshot) and the claim's lock
    time as the ranking attribution; the wallet projection matches; a
    sequential replay answers ALREADY_REVIEWED and writes nothing; the
    SUBMISSION_APPROVED audit payload is the frozen shape unchanged."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    run = uuid4().hex[:8]
    user_ids: list[UUID] = []
    task_ids: list[UUID] = []
    try:
        world = await _seed(factory, run)
        owner = world["owner"]
        student = world["student"]
        claim = world["claim"]
        submission = world["submission"]
        user_ids.extend(user.id for user in (owner, student, world["reviewer"]))
        task_ids.append(world["task"].id)
        collector = InMemoryEventCollector()

        result = await _approve(
            factory,
            collector,
            Actor(user_id=owner.id, role=Role(owner.role)),
            submission.id,
        )
        assert result.already_reviewed is False
        assert result.grant is not None
        assert result.grant.points_granted == _LOCKED_POINTS

        entries = await _reward_rows(factory, claim.id)
        assert len(entries) == 1
        entry = entries[0]
        assert entry.user_id == student.id
        assert entry.ledger_type == LedgerType.ASSIGNMENT_REWARD
        assert entry.source_type == "ASSIGNMENT_CLAIM"  # the idempotency triple
        assert entry.source_id == claim.id
        assert entry.amount == _LOCKED_POINTS  # locked basis, not the base snapshot
        assert entry.affects_balance is True
        assert entry.affects_ranking is True
        assert entry.ranking_effective_at == _LOCK_TIME  # the claim's lock time
        assert entry.reversal_of_id is None

        async with factory() as check:
            wallet = await check.get(PointWallet, student.id)
            assert wallet is not None
            assert wallet.available_points == _LOCKED_POINTS
            assert wallet.earned_points == _LOCKED_POINTS

        # The sequential replay: a fresh request-scoped adapter answers
        # the idempotent ALREADY_REVIEWED and leaves the ledger and the
        # wallet untouched.
        replay = await _approve(
            factory,
            collector,
            Actor(user_id=owner.id, role=Role(owner.role)),
            submission.id,
        )
        assert replay.already_reviewed is True
        assert replay.grant is None
        assert len(await _reward_rows(factory, claim.id)) == 1
        async with factory() as check:
            wallet = await check.get(PointWallet, student.id)
            assert wallet is not None
            assert wallet.available_points == _LOCKED_POINTS
            assert wallet.earned_points == _LOCKED_POINTS

        # The audit event payload: the frozen §14 shape, unchanged by
        # the fake-to-real swap.
        events = collector.of_type(SUBMISSION_APPROVED_EVENT)
        assert len(events) == 1
        assert events[0].aggregate_type == "AssignmentClaim"
        assert events[0].aggregate_id == claim.id
        assert events[0].occurred_at == _NOW
        assert events[0].payload == {
            "user_id": str(student.id),
            "task_id": str(world["task"].id),
            "submission_id": str(submission.id),
            "reviewer_id": str(owner.id),
            "reward_tier_locked": _LOCKED_TIER,
            "locked_reward_points": _LOCKED_POINTS,
            "terminal_at": _NOW.isoformat(),
        }
    finally:
        await _committed_cleanup(factory, user_ids=user_ids, task_ids=task_ids)


# --- the ranking-projection trigger (final-review C1) ---------------------------------


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


@pytest_asyncio.fixture
async def projection_redis() -> AsyncIterator[aioredis.Redis]:
    """Redis on the integration-test database (the rankings-suite
    fixture shape), flushed around the test: the projection's whole
    state lives in keys."""
    url = get_settings().redis_url
    parts = urlsplit(url)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"Ranking integration tests refuse non-local Redis: {url!r}")
    client = aioredis.from_url(url, decode_responses=True)
    try:
        await client.flushdb()
        yield client
        await client.flushdb()
    finally:
        await client.aclose()


@dataclass(frozen=True, slots=True)
class _EnqueuedRankingUpdate:
    """One captured ranking-projection trigger (the rankings
    ``RankingUpdateDispatcher`` payload shape)."""

    user_id: UUID
    ranking_effective_at: datetime
    request_id: str | None


class _CapturingRankingDispatcher:
    """Test fake for the ranking-projection port (the seam the
    composition root binds to ``CeleryRankingDispatcher`` in
    production): records the full post-commit payload."""

    def __init__(self) -> None:
        self.updates: list[_EnqueuedRankingUpdate] = []

    def enqueue_ranking_update(
        self,
        user_id: UUID,
        ranking_effective_at: datetime,
        request_id: str | None = None,
    ) -> None:
        self.updates.append(
            _EnqueuedRankingUpdate(user_id, ranking_effective_at, request_id)
        )


@pytest.mark.integration
async def test_approve_grant_enqueues_ranking_projection_and_boards_converge(
    db_engine: AsyncEngine,
    projection_redis: aioredis.Redis,
) -> None:
    """Final-review C1, end to end: the approve -> grant path must
    ENQUEUE the ranking-projection trigger after commit — the user, the
    entry's ``ranking_effective_at`` (the claim's lock time, so the
    recompute addresses the period the reward was credited to), and the
    grant's idempotency key as the correlation id — and RUNNING the
    real projection with that payload converges the Redis boards to the
    PostgreSQL aggregate (spec §17.2/§17.3). The idempotent replay
    approve enqueues NOTHING (nothing changed)."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    run = uuid4().hex[:8]
    user_ids: list[UUID] = []
    task_ids: list[UUID] = []
    try:
        world = await _seed(factory, run)
        owner = world["owner"]
        student = world["student"]
        claim = world["claim"]
        submission = world["submission"]
        user_ids.extend(user.id for user in (owner, student, world["reviewer"]))
        task_ids.append(world["task"].id)
        dispatcher = _CapturingRankingDispatcher()

        async with factory() as session:
            service = ReviewService(
                clock=FrozenClock(_NOW),
                events=InMemoryEventCollector(),
                points=PointsRewardPortAdapter(
                    ledger=LedgerService(), db=session, ranking_dispatcher=dispatcher
                ),
            )
            result = await service.approve_submission(
                session,
                Actor(user_id=owner.id, role=Role(owner.role)),
                submission.id,
            )
        assert result.already_reviewed is False
        assert dispatcher.updates == [
            _EnqueuedRankingUpdate(
                user_id=student.id,
                ranking_effective_at=_LOCK_TIME,
                request_id=f"assignment_reward:{claim.id}",
            )
        ]

        # The production worker's payload, run against the REAL
        # projection: the boards land at the PostgreSQL aggregate for
        # the lock-time business day/month and all-time.
        projection = RankingRedisProjection()
        async with factory() as session:
            summary = await projection.apply_ranking_update(
                session, projection_redis, student.id, _LOCK_TIME
            )
        assert summary["updated_keys"] == [
            "ranking:all",
            "ranking:daily:2026-09-01",
            "ranking:monthly:2026-09",
        ]
        member = str(student.id)
        for key in (
            "ranking:daily:2026-09-01",
            "ranking:monthly:2026-09",
            "ranking:all",
        ):
            assert await projection_redis.zscore(key, member) == _LOCKED_POINTS

        # The sequential replay approve answers ALREADY_REVIEWED and
        # enqueues NOTHING: the projection already reflects the grant.
        replay_dispatcher = _CapturingRankingDispatcher()
        async with factory() as session:
            replay_service = ReviewService(
                clock=FrozenClock(_NOW),
                events=InMemoryEventCollector(),
                points=PointsRewardPortAdapter(
                    ledger=LedgerService(),
                    db=session,
                    ranking_dispatcher=replay_dispatcher,
                ),
            )
            replay = await replay_service.approve_submission(
                session,
                Actor(user_id=owner.id, role=Role(owner.role)),
                submission.id,
            )
        assert replay.already_reviewed is True
        assert replay_dispatcher.updates == []
    finally:
        await _committed_cleanup(factory, user_ids=user_ids, task_ids=task_ids)
