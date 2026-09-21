# backend/tests/workers/test_due_notification_dispatch.py
"""Worker-level tests for the due-delivery dispatcher (plan 07 T8; spec
§25.3/§25.4; backend-engineering §12, §15).

Pinned first, against the app factory and the task objects only:

- The job module is importable by a real worker startup (JOB_MODULES).
- Signature shapes: the task receives the correlation id only, and
  `collect_due_deliveries` takes the session and the instant with
  keyword-only batch/threshold parameters.
- The batch ceiling and the stale-SENDING threshold are wired from
  Settings (scanner and claim gate share one value).

The end-to-end scenarios then run the REAL eager chain against the
real PostgreSQL test database — the scan samples SystemClock,
discovers, and the enqueued `send_notification_delivery` jobs run
inline with the production service composition — substituting only
the session maker with a NullPool factory (one asyncio.run loop per
task body must never share pooled connections across closed loops; the
same substitution pattern as tests/workers/test_expire_claims.py). All
seeded deliveries are IN_APP and non-deadline events, so the inline
sends need no provider port and no claim-status resolution: the chain
under test is exactly scan -> enqueue -> claim -> resolve.

Real commits require real cleanup: every seeded user/notification/
delivery triple is deleted in FK order at teardown.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections.abc import Iterator
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

from app.core.config import Settings
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.notifications.enums import (
    DeliveryStatus,
    NotificationChannel,
    NotificationEventType,
)
from app.modules.notifications.models import Notification, NotificationDelivery
from app.workers.celery_app import JOB_MODULES, create_celery_app
from app.workers.jobs.dispatch_due_notifications import (
    REASON_DUE,
    collect_due_deliveries,
    dispatch_due_notifications,
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
    from sqlalchemy.engine import make_url

    url = os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)
    database = make_url(url).database or ""
    if _TEST_DATABASE_MARKER not in database:
        pytest.fail(
            f"Refusing dispatch-worker tests against non-test database "
            f"{database!r} (DATABASE_URL={url!r}): the database name must "
            f"contain {_TEST_DATABASE_MARKER!r}."
        )
    return url


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
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
def db_engine() -> Iterator[AsyncEngine]:
    """NullPool engine: connections never outlive their asyncio.run loop."""
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    yield engine
    asyncio.run(engine.dispose())


@pytest.fixture
def celery_app(monkeypatch: pytest.MonkeyPatch) -> Celery:
    """TEST-ONLY eager app (same pattern as test_celery_wiring.py):
    `.delay()` runs inline — the scan's per-id send enqueues included
    — and no broker socket is ever opened."""
    _set_required_env(monkeypatch)
    app = create_celery_app(Settings())
    app.conf.task_always_eager = True
    return app


# --- row builders ------------------------------------------------------------------


def _student(run: str) -> User:
    return User(
        username=f"stu-{run}",
        password_hash=_PASSWORD_HASH,
        nickname="测试同学",
        phone_e164=None,
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )


def _notification(user: User, *, event_key: str) -> Notification:
    # SUBMISSION_APPROVED: a non-deadline event, so the inline send's
    # dispatch-time suppression never consults claim state, and IN_APP
    # needs no provider port — the chain under test stays scan-only.
    return Notification(
        user_id=user.id,
        event_key=event_key,
        event_type=NotificationEventType.SUBMISSION_APPROVED.value,
        title="任务审核通过",
        body="您的提交已通过审核，获得100积分。",
    )


def _delivery(
    notification: Notification,
    *,
    scheduled_at: datetime,
    status: DeliveryStatus = DeliveryStatus.PENDING,
    attempts: int = 0,
    updated_at: datetime | None = None,
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
    if updated_at is not None:
        # An explicit claim age for the stuck-SENDING rows (the lease
        # heuristic reads updated_at; on INSERT a client value wins
        # over the server default).
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


async def _seed_triple(
    maker: async_sessionmaker[AsyncSession],
    run: str,
    *,
    event_key: str,
    scheduled_at: datetime,
    status: DeliveryStatus = DeliveryStatus.PENDING,
    attempts: int = 0,
    updated_at: datetime | None = None,
) -> tuple[User, Notification, NotificationDelivery]:
    """Commit one user/notification/delivery triple and return it.

    One user per triple keeps every test user independent (the inbox
    rows are never asserted here, only the delivery lifecycle).
    """
    async with maker() as session:
        user = _student(run)
        session.add(user)
        await session.flush()
        notification = _notification(user, event_key=event_key)
        session.add(notification)
        await session.flush()
        delivery = _delivery(
            notification,
            scheduled_at=scheduled_at,
            status=status,
            attempts=attempts,
            updated_at=updated_at,
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


async def _count_deliveries(
    maker: async_sessionmaker[AsyncSession], event_keys: list[str]
) -> int:
    from sqlalchemy import func, select

    async with maker() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(NotificationDelivery)
                .where(NotificationDelivery.event_key.in_(event_keys))
            )
            or 0
        )


# --- pins ---------------------------------------------------------------------------


def test_dispatch_module_registered_in_job_modules() -> None:
    # A real worker process imports no test module: the job module must
    # be named in JOB_MODULES or its tasks stay invisible to startup.
    assert "app.workers.jobs.dispatch_due_notifications" in JOB_MODULES


def test_job_signatures_carry_ids_and_params_only() -> None:
    # §12: a job loads IDs and parameters, constructs dependencies,
    # then calls a service. bind=True consumes `self` (job-id logging
    # only), so producers pass the correlation id and nothing else.
    task_parameters = inspect.signature(dispatch_due_notifications).parameters
    assert list(task_parameters) == ["request_id"]
    assert all(
        parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        for parameter in task_parameters.values()
    )

    # The candidate query's inputs are the session and the instant,
    # with the batch size and lease threshold keyword-only.
    collector = inspect.signature(collect_due_deliveries).parameters
    assert list(collector) == ["session", "now", "limit", "stale_after"]
    assert collector["limit"].kind is inspect.Parameter.KEYWORD_ONLY
    assert collector["stale_after"].kind is inspect.Parameter.KEYWORD_ONLY


def test_batch_ceiling_and_lease_threshold_come_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Scanner and claim gate share ONE configured threshold; the batch
    # ceiling bounds each beat's enqueue burst (defaults: 500 / 900s).
    _set_required_env(monkeypatch)
    settings = Settings()
    assert settings.notification_dispatch_batch_limit == 500
    assert settings.notification_dispatch_stale_sending_seconds == 900
    with pytest.raises(ValueError):
        Settings(notification_dispatch_batch_limit=0)
    with pytest.raises(ValueError):
        Settings(notification_dispatch_stale_sending_seconds=0)


# --- eager end-to-end on the real database -------------------------------------------


def test_eager_dispatch_enqueues_exactly_due_deliveries(
    celery_app: Celery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real chain, inline: the scan samples SystemClock, discovers
    exactly the two due PENDING deliveries (the future one is not due),
    and each enqueued send claims and resolves its row (IN_APP: SENT
    without a provider call). A second scan discovers nothing — and no
    run ever created a new Delivery row (the send job's claim gate owns
    idempotency; the scan owns discovery only)."""
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.db.session.get_async_session_maker", lambda: maker)

    run = uuid4().hex[:8]
    real_now = datetime.now(UTC)
    seeded = _Seeded()

    async def seed() -> None:
        seeded.add(
            *await _seed_triple(
                maker,
                f"{run}a",
                event_key=f"submission:{run}a:approved",
                scheduled_at=real_now - timedelta(minutes=1),
            )
        )
        seeded.add(
            *await _seed_triple(
                maker,
                f"{run}b",
                event_key=f"submission:{run}b:approved",
                scheduled_at=real_now - timedelta(minutes=2),
            )
        )
        seeded.add(
            *await _seed_triple(
                maker,
                f"{run}c",
                event_key=f"submission:{run}c:approved",
                scheduled_at=real_now + timedelta(hours=1),
            )
        )

    asyncio.run(seed())
    try:
        first = dispatch_due_notifications.delay("req-dispatch-1").get(timeout=30)
        # Seed order: [0] is the 1-minute-ago row, [1] the 2-minutes-ago
        # row (the OLDER due instant), [2] the future one.
        newer, older, future = seeded.deliveries
        assert first["request_id"] == "req-dispatch-1"
        assert first["enqueued"] == 2
        assert first["due"] == 2
        assert first["stuck_sending"] == 0
        # Oldest due first (scheduled_at order): the 2-min-old row, then
        # the 1-min-old one; the future row is absent.
        assert first["delivery_ids"] == [str(older.id), str(newer.id)]
        # JSON wire contract: the summary round-trips through json.
        assert json.loads(json.dumps(first)) == first

        async def inspect() -> tuple[
            NotificationDelivery,
            NotificationDelivery,
            NotificationDelivery,
            int,
        ]:
            return (
                await _delivery_row(maker, older.id),
                await _delivery_row(maker, newer.id),
                await _delivery_row(maker, future.id),
                await _count_deliveries(
                    maker,
                    [row.event_key for row in seeded.deliveries],
                ),
            )

        older_row, newer_row, future_row, total = asyncio.run(inspect())
        for row in (older_row, newer_row):
            assert row.status == DeliveryStatus.SENT.value
            assert row.attempts == 1
            assert row.sent_at is not None
        assert future_row.status == DeliveryStatus.PENDING.value
        assert future_row.attempts == 0
        assert future_row.sent_at is None
        # One delivery row per event: the enqueued sends mutated their
        # rows, never inserted.
        assert total == 3

        # The second scan discovers nothing: both due rows are now
        # terminal and left the candidate set.
        second = dispatch_due_notifications.delay("req-dispatch-2").get(timeout=30)
        assert second["enqueued"] == 0
        assert second["delivery_ids"] == []
        # Still exactly three delivery rows: neither scan nor the
        # inline sends ever inserted one.
        assert (
            asyncio.run(
                _count_deliveries(maker, [row.event_key for row in seeded.deliveries])
            )
            == 3
        )
    finally:
        asyncio.run(_cleanup(maker, seeded))
    asyncio.run(engine.dispose())


def test_dispatch_re_enqueues_aged_sending_and_leaves_fresh_alone(
    celery_app: Celery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stuck-SENDING carry: an IN_FLIGHT claim older than the
    configured threshold (default 15 minutes) is presumed dead — the
    scan re-enqueues it and the send's claim gate RE-CLAIMS it (attempts
    advance). A FRESH claim is presumed live: the scan leaves the row
    alone entirely."""
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.db.session.get_async_session_maker", lambda: maker)

    run = uuid4().hex[:8]
    real_now = datetime.now(UTC)
    seeded = _Seeded()

    async def seed() -> None:
        seeded.add(
            *await _seed_triple(
                maker,
                f"{run}aged",
                event_key=f"submission:{run}aged:approved",
                scheduled_at=real_now - timedelta(minutes=30),
                status=DeliveryStatus.SENDING,
                attempts=1,
                updated_at=real_now - timedelta(minutes=20),
            )
        )
        seeded.add(
            *await _seed_triple(
                maker,
                f"{run}fresh",
                event_key=f"submission:{run}fresh:approved",
                scheduled_at=real_now - timedelta(minutes=30),
                status=DeliveryStatus.SENDING,
                attempts=1,
                updated_at=real_now - timedelta(seconds=30),
            )
        )

    asyncio.run(seed())
    try:
        scan = dispatch_due_notifications.delay("req-dispatch-stuck").get(timeout=30)
        aged, fresh = seeded.deliveries
        assert scan["enqueued"] == 1
        assert scan["due"] == 0
        assert scan["stuck_sending"] == 1
        assert scan["delivery_ids"] == [str(aged.id)]

        async def inspect() -> tuple[NotificationDelivery, NotificationDelivery]:
            return (
                await _delivery_row(maker, aged.id),
                await _delivery_row(maker, fresh.id),
            )

        aged_row, fresh_row = asyncio.run(inspect())
        # The claim gate re-claimed the stale row and delivered it: the
        # crashed sender's attempt 1 plus the re-claim's attempt 2.
        assert aged_row.status == DeliveryStatus.SENT.value
        assert aged_row.attempts == 2
        assert aged_row.sent_at is not None
        # The fresh claim was never re-enqueued, so never touched.
        assert fresh_row.status == DeliveryStatus.SENDING.value
        assert fresh_row.attempts == 1
        assert fresh_row.sent_at is None
    finally:
        asyncio.run(_cleanup(maker, seeded))
    asyncio.run(engine.dispose())


# --- scan predicate, directly (no Celery) ---------------------------------------------


async def test_collect_due_deliveries_predicate(db_engine: AsyncEngine) -> None:
    """The candidate read itself: a due PENDING row is discovered with
    reason `due`; a RETRYABLE row still waiting for its next ladder
    rung is NOT (the scan re-enters at the due instant only); FAILED
    and future rows are never re-enqueued."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    real_now = datetime.now(UTC)
    seeded = _Seeded()
    run = uuid4().hex[:8]

    async def seed() -> NotificationDelivery:
        seeded.add(
            *await _seed_triple(
                maker,
                f"{run}a",
                event_key=f"submission:{run}a:approved",
                scheduled_at=real_now - timedelta(minutes=1),
            )
        )
        seeded.add(
            *await _seed_triple(
                maker,
                f"{run}b",
                event_key=f"submission:{run}b:approved",
                scheduled_at=real_now + timedelta(minutes=5),
                status=DeliveryStatus.RETRYABLE,
                attempts=1,
            )
        )
        seeded.add(
            *await _seed_triple(
                maker,
                f"{run}c",
                event_key=f"submission:{run}c:approved",
                scheduled_at=real_now - timedelta(minutes=1),
                status=DeliveryStatus.FAILED,
                attempts=3,
            )
        )
        return seeded.deliveries[0]

    due = await seed()
    try:
        async with maker() as session:
            discovered = await collect_due_deliveries(
                session,
                datetime.now(UTC) + timedelta(seconds=1),
                limit=10,
                stale_after=timedelta(minutes=15),
            )
        discovered_map = {str(item.delivery_id): item.reason for item in discovered}
        assert discovered_map[str(due.id)] == REASON_DUE
        for row in seeded.deliveries[1:]:
            assert str(row.id) not in discovered_map
        assert set(discovered_map.values()) == {REASON_DUE}
    finally:
        # Async test: no asyncio.run here (a loop is already running).
        await _cleanup(maker, seeded)
