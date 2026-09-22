# backend/tests/workers/test_recover_stuck_sending.py
"""Worker-level tests for the aged-SENDING recovery monitor (Plan 08
W5a carry; the stuck-SENDING ruling).

Pinned first, against the app factory and the task objects only:

- The job module is importable by a real worker startup (JOB_MODULES).
- Signature shapes: the task receives the correlation id only, and
  ``recover_stuck_sending_deliveries`` takes the session and the
  SERVICE-clock instant with keyword-only threshold/batch parameters.
- The threshold and the beat cadence are wired from Settings, with
  the >= 1s guards that keep the heuristic from becoming a hot loop
  over live claims.

The behavioral scenarios run against the real PostgreSQL test
database. The threshold-boundary suite drives the core UPDATE with a
``FrozenClock`` instant and exact-microsecond claim ages: a row at the
cutoff is recovered (``<=``), one microsecond newer is not. The eager
suite runs the REAL task body (settings wiring, per-job session
source, SystemClock) and then the REAL dispatcher, proving the whole
loop the monitor exists for: wedged SENDING -> RETRYABLE -> next due
scan re-dispatches -> delivered. The division-of-labor test pins the
monitor (automatic, RETRYABLE) against the operator command
(``RepairService.force_fail_delivery``, FAILED): different states,
no conflict.

Real commits require real cleanup: every seeded user/notification/
delivery triple is deleted in FK order at teardown.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from celery import Celery
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.clock import FrozenClock
from app.core.config import Settings, get_settings
from app.modules.audit.repair_service import FORCED_LAST_ERROR_PREFIX, RepairService
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.notifications.enums import DeliveryStatus, NotificationChannel
from app.modules.notifications.models import Notification, NotificationDelivery
from app.workers.celery_app import JOB_MODULES, create_celery_app
from app.workers.jobs.dispatch_due_notifications import dispatch_due_notifications
from app.workers.jobs.recover_stuck_sending import (
    STUCK_SENDING_RECOVERY_MARKER,
    recover_stuck_sending,
    recover_stuck_sending_deliveries,
)

pytestmark = pytest.mark.integration

# --- test database guard (mirrors tests/integration/db_guard.py) --------------------

_TEST_DATABASE_MARKER = "campusquest_test"
_DEFAULT_DATABASE_URL = (
    "postgresql+asyncpg://test:test@localhost:15432/campusquest_test"
)

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)


def _database_url() -> str:
    """The integration database URL, refusing non-test databases."""
    import os

    from sqlalchemy.engine import make_url

    url = os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)
    database = make_url(url).database or ""
    if _TEST_DATABASE_MARKER not in database:
        pytest.fail(
            f"Refusing recovery-worker tests against non-test database "
            f"{database!r} (DATABASE_URL={url!r}): the database name must "
            f"contain {_TEST_DATABASE_MARKER!r}."
        )
    return url


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for name, value in {
        "DATABASE_URL": _database_url(),
        "REDIS_URL": os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        "S3_ENDPOINT_URL": "http://localhost:9000",
        "S3_BUCKET": "campusquest-test",
        "S3_ACCESS_KEY": "campusquest",
        "S3_SECRET_KEY": "campusquest-dev",
        "BUSINESS_TIMEZONE": "Asia/Shanghai",
    }.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def db_engine() -> AsyncEngine:
    """NullPool engine: connections never outlive their asyncio.run loop."""
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    yield engine
    asyncio.run(engine.dispose())


@pytest.fixture
def celery_app(monkeypatch: pytest.MonkeyPatch) -> Celery:
    """TEST-ONLY eager app (same pattern as test_celery_wiring.py):
    `.delay()` runs inline and no broker socket is ever opened."""
    _set_required_env(monkeypatch)
    app = create_celery_app(Settings())
    app.conf.task_always_eager = True
    return app


# --- row builders ------------------------------------------------------------------


def _student(run: str) -> User:
    return User(
        username=f"stu-{run}",
        password_hash=_PASSWORD_HASH,
        nickname="恢复测试同学",
        phone_e164=None,
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )


def _notification(user: User, *, event_key: str) -> Notification:
    return Notification(
        user_id=user.id,
        event_key=event_key,
        event_type="SUBMISSION_APPROVED",
        title="任务审核通过",
        body="您的提交已通过审核，获得100积分。",
    )


def _delivery(
    notification: Notification,
    *,
    status: DeliveryStatus,
    updated_at: datetime,
    scheduled_at: datetime,
    attempts: int = 1,
) -> NotificationDelivery:
    delivery = NotificationDelivery(
        notification_id=notification.id,
        user_id=notification.user_id,
        event_key=notification.event_key,
        channel=NotificationChannel.IN_APP.value,
        status=status.value,
        scheduled_at=scheduled_at,
        attempts=attempts,
    )
    # An explicit claim age for the SENDING rows (the lease heuristic
    # reads updated_at; on INSERT a client value wins over the server
    # default).
    delivery.updated_at = updated_at
    return delivery


class _Seeded:
    """The committed rows one test seeded (for FK-ordered teardown)."""

    def __init__(self) -> None:
        self.users: list[User] = []
        self.notifications: list[Notification] = []
        self.deliveries: list[NotificationDelivery] = []

    def add(
        self, user: User, notification: Notification, delivery: NotificationDelivery
    ) -> None:
        self.users.append(user)
        self.notifications.append(notification)
        self.deliveries.append(delivery)


async def _seed(
    maker: async_sessionmaker[AsyncSession],
    run: str,
    *,
    event_key: str,
    status: DeliveryStatus,
    updated_at: datetime,
    scheduled_at: datetime,
    attempts: int = 1,
) -> tuple[User, Notification, NotificationDelivery]:
    """Commit one user/notification/delivery triple and return it."""
    async with maker() as session:
        user = _student(run)
        session.add(user)
        await session.flush()
        notification = _notification(user, event_key=event_key)
        session.add(notification)
        await session.flush()
        delivery = _delivery(
            notification,
            status=status,
            updated_at=updated_at,
            scheduled_at=scheduled_at,
            attempts=attempts,
        )
        session.add(delivery)
        await session.commit()
        return user, notification, delivery


async def _cleanup(maker: async_sessionmaker[AsyncSession], seeded: _Seeded) -> None:
    """Explicit committed cleanup in FK order; real commits leave no
    outer rollback to trust."""
    async with maker() as session:
        if seeded.deliveries:
            await session.execute(
                delete(NotificationDelivery).where(
                    NotificationDelivery.id.in_([row.id for row in seeded.deliveries])
                )
            )
        if seeded.notifications:
            await session.execute(
                delete(Notification).where(
                    Notification.id.in_([row.id for row in seeded.notifications])
                )
            )
        if seeded.users:
            await session.execute(
                delete(User).where(User.id.in_([row.id for row in seeded.users]))
            )
        await session.commit()


async def _delivery_row(
    maker: async_sessionmaker[AsyncSession], delivery_id: UUID
) -> NotificationDelivery:
    async with maker() as session:
        delivery = await session.get(NotificationDelivery, delivery_id)
        assert delivery is not None
        return delivery


# --- pins ---------------------------------------------------------------------------


def test_recovery_module_registered_in_job_modules() -> None:
    # A real worker process imports no test module: the job module must
    # be named in JOB_MODULES or its tasks stay invisible to startup.
    assert "app.workers.jobs.recover_stuck_sending" in JOB_MODULES


def test_job_signatures_carry_ids_and_params_only() -> None:
    # §12: bind=True consumes `self` (job-id logging only), so
    # producers pass the correlation id and nothing else. The recovery
    # UPDATE's inputs are the session and the SERVICE-clock instant,
    # with the threshold and batch keyword-only.
    task_parameters = inspect.signature(recover_stuck_sending).parameters
    assert list(task_parameters) == ["request_id"]
    assert all(
        parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        for parameter in task_parameters.values()
    )

    recover = inspect.signature(recover_stuck_sending_deliveries).parameters
    assert list(recover) == ["session", "now", "stuck_after", "limit"]
    assert recover["stuck_after"].kind is inspect.Parameter.KEYWORD_ONLY
    assert recover["limit"].kind is inspect.Parameter.KEYWORD_ONLY


def test_threshold_and_cadence_come_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The monitor's lease threshold (default 600s — above any healthy
    # provider call, below the dispatcher's 900s re-enqueue heuristic)
    # and its beat cadence are Settings fields; a sub-second threshold
    # or cadence is refused at settings load.
    _set_required_env(monkeypatch)
    settings = Settings()
    assert settings.notification_sending_stuck_threshold_seconds == 600
    assert settings.notification_sending_stuck_scan_interval_seconds == 60
    with pytest.raises(ValueError):
        Settings(notification_sending_stuck_threshold_seconds=0)
    with pytest.raises(ValueError):
        Settings(notification_sending_stuck_scan_interval_seconds=0)


# --- threshold boundary, FrozenClock (direct core call) ------------------------------


async def test_threshold_boundary_recovers_at_and_beyond_cutoff(
    db_engine: AsyncEngine,
) -> None:
    """Exact-microsecond boundary: a SENDING claim aged exactly the
    threshold is recovered (``updated_at <= cutoff``); one microsecond
    newer is not. The recovered row carries RETRYABLE, the marker
    last_error, and the SERVICE-clock stamp — scheduled_at and attempts
    untouched (the next claim advances them). A same-instant re-run
    (beat redelivery) recovers nothing: the flipped rows no longer
    match the predicate."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    now = FrozenClock(datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)).now()
    threshold = timedelta(seconds=600)
    cutoff = now - threshold
    seeded = _Seeded()
    run = uuid4().hex[:8]

    async def seed() -> None:
        seeded.add(
            *await _seed(
                maker,
                f"{run}aged",
                event_key=f"submission:{run}aged:approved",
                status=DeliveryStatus.SENDING,
                updated_at=cutoff - timedelta(seconds=1),
                scheduled_at=now - timedelta(minutes=30),
            )
        )
        seeded.add(
            *await _seed(
                maker,
                f"{run}boundary",
                event_key=f"submission:{run}boundary:approved",
                status=DeliveryStatus.SENDING,
                updated_at=cutoff,
                scheduled_at=now - timedelta(minutes=30),
            )
        )
        seeded.add(
            *await _seed(
                maker,
                f"{run}fresh",
                event_key=f"submission:{run}fresh:approved",
                status=DeliveryStatus.SENDING,
                updated_at=cutoff + timedelta(microseconds=1),
                scheduled_at=now - timedelta(minutes=30),
            )
        )

    await seed()
    try:
        async with maker() as session:
            recovered = await recover_stuck_sending_deliveries(
                session, now, stuck_after=threshold, limit=10
            )
        aged, boundary, fresh = seeded.deliveries
        assert set(recovered) == {aged.id, boundary.id}

        aged_row = await _delivery_row(maker, aged.id)
        boundary_row = await _delivery_row(maker, boundary.id)
        fresh_row = await _delivery_row(maker, fresh.id)
        for row in (aged_row, boundary_row):
            assert row.status == DeliveryStatus.RETRYABLE.value
            assert row.last_error == STUCK_SENDING_RECOVERY_MARKER
            # The SERVICE-clock instant, not a database stamp.
            assert row.updated_at == now
            assert row.attempts == 1
            assert row.sent_at is None
        assert aged_row.scheduled_at == aged.scheduled_at
        # One microsecond inside the lease: untouched in every column.
        assert fresh_row.status == DeliveryStatus.SENDING.value
        assert fresh_row.last_error is None
        assert fresh_row.updated_at == cutoff + timedelta(microseconds=1)

        # Same-instant redelivery (a duplicate beat instance) matches
        # nothing: the flipped rows left the SENDING candidate set.
        async with maker() as session:
            again = await recover_stuck_sending_deliveries(
                session, now, stuck_after=threshold, limit=10
            )
        assert again == []
    finally:
        await _cleanup(maker, seeded)


async def test_only_sending_rows_are_recovery_candidates(
    db_engine: AsyncEngine,
) -> None:
    """The status predicate is the safety rail: PENDING, RETRYABLE,
    SENT, and FAILED rows — however old — are never touched by the
    monitor (they are the dispatcher's and the terminal states, not
    the wedge)."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    now = FrozenClock(datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)).now()
    threshold = timedelta(seconds=600)
    seeded = _Seeded()
    run = uuid4().hex[:8]

    for index, status in enumerate(DeliveryStatus):
        if status is DeliveryStatus.SENDING:
            continue
        seeded.add(
            *await _seed(
                maker,
                f"{run}{index}",
                event_key=f"submission:{run}{index}:approved",
                status=status,
                updated_at=now - timedelta(hours=1),
                scheduled_at=now - timedelta(hours=1),
            )
        )
    try:
        async with maker() as session:
            recovered = await recover_stuck_sending_deliveries(
                session, now, stuck_after=threshold, limit=10
            )
        assert recovered == []
        for delivery in seeded.deliveries:
            row = await _delivery_row(maker, delivery.id)
            assert row.status == delivery.status
            assert row.last_error is None
    finally:
        await _cleanup(maker, seeded)


# --- eager end-to-end on the real database -------------------------------------------


def test_eager_monitor_recovers_then_dispatcher_redispatches(
    celery_app: Celery,
) -> None:
    """The real chain, inline: the monitor task samples SystemClock,
    flips the aged SENDING row (its lease is 20 minutes old, far past
    the 600s threshold) to RETRYABLE, leaves the fresh claim (30s)
    alone, and enqueues NOTHING itself — the NEXT dispatcher round
    re-dispatches the recovered row as ordinary due work (IN_APP: the
    inline send resolves SENT without a provider call). A second
    monitor run recovers nothing."""
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    # The REAL task code runs un-substituted: each task body builds its
    # own per-invocation engine through the shared session source
    # against this same test DATABASE_URL (the settings cache is
    # cleared around the run so no stale process-wide snapshot
    # survives — the test_submission_validation_job.py pattern).
    get_settings.cache_clear()

    run = uuid4().hex[:8]
    real_now = datetime.now(UTC)
    seeded = _Seeded()

    def seed() -> None:
        asyncio.run(
            _seed_one(maker, seeded, run, "aged", real_now - timedelta(minutes=20))
        )
        asyncio.run(
            _seed_one(maker, seeded, run, "fresh", real_now - timedelta(seconds=30))
        )

    seed()
    try:
        monitor = recover_stuck_sending.delay("req-stuck-1").get(timeout=30)
        aged, fresh = seeded.deliveries
        assert monitor["request_id"] == "req-stuck-1"
        assert monitor["recovered"] == 1
        assert monitor["delivery_ids"] == [str(aged.id)]

        def inspect() -> tuple[NotificationDelivery, NotificationDelivery]:
            return asyncio.run(_fetch_pair(maker, aged.id, fresh.id))

        aged_row, fresh_row = inspect()
        assert aged_row.status == DeliveryStatus.RETRYABLE.value
        assert aged_row.last_error == STUCK_SENDING_RECOVERY_MARKER
        assert aged_row.attempts == 1
        assert aged_row.sent_at is None
        assert fresh_row.status == DeliveryStatus.SENDING.value
        assert fresh_row.attempts == 1

        # The monitor enqueues nothing; the dispatcher owns re-dispatch.
        dispatch = dispatch_due_notifications.delay("req-stuck-2").get(timeout=30)
        assert str(aged.id) in dispatch["delivery_ids"]
        assert str(fresh.id) not in dispatch["delivery_ids"]

        aged_row, fresh_row = inspect()
        assert aged_row.status == DeliveryStatus.SENT.value
        assert aged_row.attempts == 2  # the crashed claim + the re-dispatch
        assert aged_row.sent_at is not None
        assert fresh_row.status == DeliveryStatus.SENDING.value

        again = recover_stuck_sending.delay("req-stuck-3").get(timeout=30)
        assert again["recovered"] == 0
        assert again["delivery_ids"] == []
    finally:
        asyncio.run(_cleanup(maker, seeded))
        get_settings.cache_clear()
    asyncio.run(engine.dispose())


async def _seed_one(
    maker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    run: str,
    suffix: str,
    updated_at: datetime,
) -> None:
    seeded.add(
        *await _seed(
            maker,
            f"{run}{suffix}",
            event_key=f"submission:{run}{suffix}:approved",
            status=DeliveryStatus.SENDING,
            updated_at=updated_at,
            scheduled_at=updated_at - timedelta(minutes=10),
        )
    )


async def _fetch_pair(
    maker: async_sessionmaker[AsyncSession], aged_id: UUID, fresh_id: UUID
) -> tuple[NotificationDelivery, NotificationDelivery]:
    return (
        await _delivery_row(maker, aged_id),
        await _delivery_row(maker, fresh_id),
    )


# --- division of labor with the manual force-fail command ----------------------------


async def test_monitor_and_force_fail_land_different_states(
    db_engine: AsyncEngine,
) -> None:
    """The automatic path and the operator path coexist without
    conflict: the monitor flips the AGED wedged row to RETRYABLE
    (re-try semantics; the dispatcher will re-dispatch it), while
    ``force_fail_delivery`` flips the still-SENDING row to FAILED
    (kill semantics, ``forced:`` annotation). Each writes its own row
    under its own lock, and each state is the other's non-target: the
    monitor's predicate is SENDING-past-threshold and the command's is
    SENDING, so neither can overwrite the other's outcome."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    now = FrozenClock(datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)).now()
    threshold = timedelta(seconds=600)
    seeded = _Seeded()
    run = uuid4().hex[:8]

    async def seed() -> None:
        # The automatic path's target: a wedged claim past the lease
        # threshold.
        seeded.add(
            *await _seed(
                maker,
                f"{run}auto",
                event_key=f"submission:{run}auto:approved",
                status=DeliveryStatus.SENDING,
                updated_at=now - timedelta(minutes=20),
                scheduled_at=now - timedelta(minutes=30),
            )
        )
        # The operator path's target: still SENDING (a fresh lease the
        # monitor rightly refuses to scoop).
        seeded.add(
            *await _seed(
                maker,
                f"{run}manual",
                event_key=f"submission:{run}manual:approved",
                status=DeliveryStatus.SENDING,
                updated_at=now - timedelta(seconds=30),
                scheduled_at=now - timedelta(minutes=30),
            )
        )

    await seed()
    try:
        async with maker() as session:
            recovered = await recover_stuck_sending_deliveries(
                session, now, stuck_after=threshold, limit=10
            )
        auto, manual = seeded.deliveries
        assert recovered == [auto.id]

        async with maker() as session:
            await RepairService(clock=FrozenClock(now)).force_fail_delivery(
                session,
                Actor(user_id=uuid4(), role=Role.ADMIN),
                manual.id,
                reason="人工终止卡死投递",
            )

        auto_row = await _delivery_row(maker, auto.id)
        manual_row = await _delivery_row(maker, manual.id)
        assert auto_row.status == DeliveryStatus.RETRYABLE.value
        assert auto_row.last_error == STUCK_SENDING_RECOVERY_MARKER
        assert manual_row.status == DeliveryStatus.FAILED.value
        assert manual_row.last_error == (f"{FORCED_LAST_ERROR_PREFIX}人工终止卡死投递")
    finally:
        await _cleanup(maker, seeded)
