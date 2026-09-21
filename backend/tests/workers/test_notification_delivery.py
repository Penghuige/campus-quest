# backend/tests/workers/test_notification_delivery.py
"""Worker-level tests for idempotent notification delivery (spec
§25.3/§25.4; plan 07 T4).

These tests pin the DeliveryService + Celery shell contract against a
real PostgreSQL test database (real commits, real row locks — the
claim-before-send race cannot be simulated on fakes):

- Duplicate job: the same delivery queued twice produces exactly one
  provider message and one SENT row (spec §25.3 "Celery 重试不得造成双发").
- Bounded retry: three TemporaryProviderError attempts end FAILED with
  attempts=3; the RETRYABLE scheduled_at instants follow the spec §25.4
  ladder (~1m, ~5m; the ~20m rung exists for configured deeper ladders).
- Unknown outcome (timeout): RETRYABLE on the SAME row with the SAME
  deterministic provider idempotency key; never a second delivery row.
- Permanent rejection: FAILED immediately, one attempt, reason recorded.
- Policy skips (unverified email, suppressed deadline claim) are NOT
  failures: the row resolves SENT with a "skipped:<token>" last_error
  and the provider port is never called.
- IN_APP: the Notification row is the inbox message (no provider port);
  completing the delivery clears read_at.
- Claim-before-send race: two concurrent sends on independent
  connections produce one provider call; the loser observes the claim.
- Stuck-SENDING lease heuristic (plan 07 T8 carry, V1 = timestamp over
  updated_at): a FRESH SENDING claim still answers IN_FLIGHT untouched;
  a STALE one (older than the threshold) is re-claimed and delivered,
  attempts advancing on the re-claim.
- Lease clock domain (plan 07 final review I1): updated_at stays in the
  SERVICE clock through claim AND finalize — the column has no ORM
  onupdate, so a finalize re-assigning the claim's instant never lets
  the DB clock overwrite it.
- Not-due deliveries are left PENDING and unclaimed.

Real commits require real cleanup: every seeded user/notification/
delivery triple is deleted in teardown (the shared outer-rollback
conftest pattern cannot apply here because the service must observe
committed rows through its own sessions).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from celery import Celery
from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.clock import Clock, FrozenClock
from app.core.config import Settings, get_settings
from app.integrations.email import LoggingEmailSender
from app.integrations.errors import (
    PermanentProviderError,
    TemporaryProviderError,
    UnknownOutcomeError,
)
from app.integrations.sms import LoggingSmsSender
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.notifications.delivery_service import (
    RETRY_DELAYS,
    DeliveryService,
    SendOutcome,
    provider_idempotency_key,
)
from app.modules.notifications.enums import (
    DeliveryStatus,
    NotificationChannel,
    NotificationEventType,
)
from app.modules.notifications.models import (
    Notification,
    NotificationDelivery,
)
from app.workers.celery_app import create_celery_app
from app.workers.jobs import send_notification as send_notification_job
from tests.fakes.integrations import FakeEmailSender, FakeSmsSender

pytestmark = pytest.mark.integration

# --- test database guard (mirrors tests/integration/db_guard.py) -----------------

_TEST_DATABASE_MARKER = "campusquest_test"
_DEFAULT_DATABASE_URL = (
    "postgresql+asyncpg://test:test@localhost:15432/campusquest_test"
)

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)

_T0 = datetime(2026, 9, 21, 8, 0, tzinfo=UTC)
_ONE_MINUTE = timedelta(minutes=1)
_FIVE_MINUTES = timedelta(minutes=5)
_TWENTY_MINUTES = timedelta(minutes=20)


def _database_url() -> str:
    """The integration database URL, refusing non-test databases."""
    url = os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)
    database = make_url(url).database or ""
    if _TEST_DATABASE_MARKER not in database:
        pytest.fail(
            f"Refusing delivery-worker tests against non-test database "
            f"{database!r} (DATABASE_URL={url!r}): the database name must "
            f"contain {_TEST_DATABASE_MARKER!r}."
        )
    return url


class StepClock:
    """Mutable test clock: the caller owns "now" (backend-engineering §11)."""

    def __init__(self, start: datetime) -> None:
        self.current = start.astimezone(UTC)

    def advance(self, delta: timedelta) -> None:
        self.current += delta

    def now(self) -> datetime:
        return self.current


# --- row builders -----------------------------------------------------------------


def _student(**overrides: Any) -> User:
    fields: dict[str, Any] = {
        "username": f"stu-{uuid4().hex[:12]}",
        "password_hash": _PASSWORD_HASH,
        "nickname": "测试同学",
        "phone_e164": "+8613800000000",
        "role": Role.STUDENT,
        "status": UserStatus.ACTIVE,
    }
    fields.update(overrides)
    return User(**fields)


def _notification(
    user: User, *, event_type: NotificationEventType, event_key: str
) -> Notification:
    return Notification(
        user_id=user.id,
        event_key=event_key,
        event_type=event_type,
        title="任务将于4小时后截止",
        body="您领取的任务将于4小时后截止，请尽快提交。",
    )


def _delivery(
    notification: Notification,
    *,
    channel: NotificationChannel,
    scheduled_at: datetime,
    status: DeliveryStatus = DeliveryStatus.PENDING,
    attempts: int = 0,
) -> NotificationDelivery:
    return NotificationDelivery(
        notification_id=notification.id,
        user_id=notification.user_id,
        event_key=notification.event_key,
        channel=channel,
        status=status,
        scheduled_at=scheduled_at,
        attempts=attempts,
    )


async def _seed_rows(
    engine: AsyncEngine,
    *,
    user_overrides: dict[str, Any] | None = None,
    event_type: NotificationEventType = NotificationEventType.ASSIGNMENT_DEADLINE_4H,
    event_key: str | None = None,
    channel: NotificationChannel = NotificationChannel.SMS,
    scheduled_at: datetime | None = None,
    notification_overrides: dict[str, Any] | None = None,
    delivery_overrides: dict[str, Any] | None = None,
) -> tuple[User, Notification, NotificationDelivery]:
    """Commit a user/notification/delivery triple and return it.

    Real commits: the service under test opens its own sessions, so the
    rows must be visible outside any seeding transaction. Ids are
    server-generated: each parent flushes before the child that
    denormalizes it (same ordering as the constraints tests). Callers
    own `_cleanup_rows`.
    """
    maker = async_sessionmaker(engine, expire_on_commit=False)
    user = _student(**(user_overrides or {}))
    key = event_key if event_key is not None else f"claim:{uuid4()}:deadline_4h"
    async with maker() as session:
        session.add(user)
        await session.flush()
        notification = _notification(user, event_type=event_type, event_key=key)
        if notification_overrides:
            for name, value in notification_overrides.items():
                setattr(notification, name, value)
        session.add(notification)
        await session.flush()
        delivery = _delivery(
            notification,
            channel=channel,
            scheduled_at=(
                scheduled_at if scheduled_at is not None else _T0 - _ONE_MINUTE
            ),
        )
        if delivery_overrides:
            for name, value in delivery_overrides.items():
                setattr(delivery, name, value)
        session.add(delivery)
        await session.commit()
    return user, notification, delivery


async def _cleanup_rows(
    engine: AsyncEngine,
    user: User,
    notification: Notification,
    delivery: NotificationDelivery,
) -> None:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await session.execute(
            delete(NotificationDelivery).where(NotificationDelivery.id == delivery.id)
        )
        await session.execute(
            delete(Notification).where(Notification.id == notification.id)
        )
        await session.execute(delete(User).where(User.id == user.id))
        await session.commit()


@asynccontextmanager
async def _seeded(
    engine: AsyncEngine,
    *,
    user_overrides: dict[str, Any] | None = None,
    event_type: NotificationEventType = NotificationEventType.ASSIGNMENT_DEADLINE_4H,
    event_key: str | None = None,
    channel: NotificationChannel = NotificationChannel.SMS,
    scheduled_at: datetime | None = None,
    notification_overrides: dict[str, Any] | None = None,
    delivery_overrides: dict[str, Any] | None = None,
) -> AsyncIterator[tuple[User, Notification, NotificationDelivery]]:
    """`_seed_rows` with teardown (for tests that stay in one event loop;
    the sync Celery test spans several loops and calls the helpers
    itself)."""
    user, notification, delivery = await _seed_rows(
        engine,
        user_overrides=user_overrides,
        event_type=event_type,
        event_key=event_key,
        channel=channel,
        scheduled_at=scheduled_at,
        notification_overrides=notification_overrides,
        delivery_overrides=delivery_overrides,
    )
    try:
        yield user, notification, delivery
    finally:
        await _cleanup_rows(engine, user, notification, delivery)


@pytest.fixture
def db_engine() -> Iterator[AsyncEngine]:
    """NullPool engine: connections never outlive their asyncio.run loop.

    The sync duplicate-job test drives the same engine from several
    short-lived loops (seed, eager job bodies, inspection); NullPool
    opens a fresh connection per session so no connection is reused
    across a closed loop.
    """
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    yield engine
    asyncio.run(engine.dispose())


def _service(
    engine: AsyncEngine,
    *,
    sms: FakeSmsSender | None = None,
    email: FakeEmailSender | None = None,
    clock: Clock | None = None,
    claim_status_resolver: Any = None,
) -> DeliveryService:
    return DeliveryService(
        session_maker=async_sessionmaker(engine, expire_on_commit=False),
        sms_sender=sms if sms is not None else FakeSmsSender(),
        email_sender=email if email is not None else FakeEmailSender(),
        clock=clock if clock is not None else StepClock(_T0),
        claim_status_resolver=claim_status_resolver,
    )


async def _row(engine: AsyncEngine, delivery_id: UUID) -> NotificationDelivery:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        delivery = await session.get(NotificationDelivery, delivery_id)
        assert delivery is not None
        return delivery


async def _delivery_count(engine: AsyncEngine, event_key: str) -> int:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(NotificationDelivery)
                .where(NotificationDelivery.event_key == event_key)
            )
            or 0
        )


async def _notification_row(engine: AsyncEngine, notification_id: UUID) -> Notification:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        notification = await session.get(Notification, notification_id)
        assert notification is not None
        return notification


# --- duplicate job (Celery eager; spec §25.3) --------------------------------------


@pytest.fixture
def celery_app(monkeypatch: pytest.MonkeyPatch) -> Celery:
    """TEST-ONLY eager app (same pattern as test_celery_wiring.py):
    `.delay()` runs inline, no broker contact."""
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
    app = create_celery_app(Settings())
    app.conf.task_always_eager = True
    return app


def test_duplicate_job_delivers_exactly_once(
    celery_app: Celery, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    sms = FakeSmsSender()
    service = _service(engine, sms=sms, clock=StepClock(_T0))
    # The eager job runs inline in THIS process, so the test-side service
    # (fake senders, frozen clock) stands in for the production wiring;
    # the per-job session maker the shell now passes in is ignored.
    monkeypatch.setattr(
        send_notification_job, "build_delivery_service", lambda **_: service
    )
    event_key = f"claim:{uuid4()}:deadline_4h"

    async def seed() -> tuple[User, Notification, NotificationDelivery]:
        return await _seed_rows(engine, event_key=event_key)

    # One asyncio.run per phase: NullPool keeps connections loop-local.
    user, notification, delivery = asyncio.run(seed())
    try:
        first = send_notification_job.send_notification_delivery.delay(
            str(delivery.id), "req-dup-1"
        ).get(timeout=10)
        second = send_notification_job.send_notification_delivery.delay(
            str(delivery.id), "req-dup-2"
        ).get(timeout=10)

        async def inspect() -> tuple[NotificationDelivery, int]:
            return (
                await _row(engine, delivery.id),
                await _delivery_count(engine, event_key),
            )

        row, count = asyncio.run(inspect())
    finally:
        asyncio.run(_cleanup_rows(engine, user, notification, delivery))
    asyncio.run(engine.dispose())

    assert first == {
        "delivery_id": str(delivery.id),
        "outcome": "SENT",
        "status": "SENT",
        "attempts": 1,
    }
    # The duplicate job is an idempotent no-op, not an error.
    assert second["outcome"] == "ALREADY_SENT"
    assert second["attempts"] == 1

    assert len(sms.messages) == 1
    assert sms.messages[0].to == "+8613800000000"
    assert sms.messages[0].template == "assignment_deadline_4h"
    assert sms.messages[0].variables == {
        "title": notification.title,
        "body": notification.body,
    }
    # The provider idempotency key is the event/channel/user identity:
    # deterministic, so any retry (or an UnknownOutcome re-send against a
    # real provider) collapses onto the same provider-side message.
    expected_key = f"{event_key}:SMS:{delivery.user_id}"
    assert sms.messages[0].idempotency_key == expected_key
    assert sms.messages[0].idempotency_key == provider_idempotency_key(
        event_key, NotificationChannel.SMS, delivery.user_id
    )

    assert row.status == DeliveryStatus.SENT.value
    assert row.attempts == 1
    assert row.sent_at == _T0
    assert row.provider_message_id is None
    assert row.last_error is None
    # One row per (event_key, user, channel), duplicate job or not.
    assert count == 1


# --- development logging semantics (PR #2 hardening P0-2, fail-closed) ------------


async def test_logging_senders_record_prefixed_provider_message_id(
    db_engine: AsyncEngine,
) -> None:
    # development runs explicitly on the logging provider (production
    # refuses it at Settings construction, config.py's production guard).
    # The recorded delivery must carry the "logging:"<uuid> receipt so
    # operations can tell a simulated send from a real provider receipt
    # at a glance — the success label stays honest about WHAT sent it.
    service = DeliveryService(
        session_maker=async_sessionmaker(db_engine, expire_on_commit=False),
        sms_sender=LoggingSmsSender(),
        email_sender=LoggingEmailSender(),
        clock=StepClock(_T0),
    )

    async with _seeded(db_engine) as sms_seeded:
        sms_delivery = sms_seeded[2]

        sms_result = await service.send(sms_delivery.id, "req-logging-sms")
        assert sms_result.outcome is SendOutcome.SENT
        sms_row = await _row(db_engine, sms_delivery.id)
        assert sms_row.status == DeliveryStatus.SENT.value
        assert sms_row.provider_message_id is not None
        assert sms_row.provider_message_id.startswith("logging:")
        # A real UUID rides behind the prefix, unique per send like a
        # provider receipt.
        UUID(sms_row.provider_message_id.removeprefix("logging:"))

    async with _seeded(
        db_engine,
        user_overrides={
            "phone_e164": None,
            "email_normalized": "student@campus.example.edu",
            "email_verified_at": _T0,
        },
        event_type=NotificationEventType.SUBMISSION_APPROVED,
        event_key=f"submission:{uuid4()}:approved",
        channel=NotificationChannel.EMAIL,
    ) as email_seeded:
        email_delivery = email_seeded[2]

        email_result = await service.send(email_delivery.id, "req-logging-email")
        assert email_result.outcome is SendOutcome.SENT
        email_row = await _row(db_engine, email_delivery.id)
        assert email_row.status == DeliveryStatus.SENT.value
        assert email_row.provider_message_id is not None
        assert email_row.provider_message_id.startswith("logging:")
        UUID(email_row.provider_message_id.removeprefix("logging:"))

    assert sms_row.provider_message_id != email_row.provider_message_id


def test_build_delivery_service_wires_logging_senders_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The worker composition point resolves the SMS/EMAIL adapters from
    # settings (`build_sms_sender`/`build_email_sender`); V1/development
    # values are "logging", so the built service carries the Logging
    # adapters. Production never gets this far with logging providers:
    # Settings construction fails closed first (config.py's production
    # guard, pinned in tests/unit/core/test_config.py).
    for name, value in {
        "DATABASE_URL": "postgresql+asyncpg://test:test@localhost:15433/"
        "campusquest_test",
        "REDIS_URL": "redis://localhost:6379/0",
        "S3_ENDPOINT_URL": "http://localhost:9000",
        "S3_BUCKET": "campusquest-test",
        "S3_ACCESS_KEY": "campusquest",
        "S3_SECRET_KEY": "campusquest-dev",
        "BUSINESS_TIMEZONE": "Asia/Shanghai",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        service = send_notification_job.build_delivery_service(
            session_maker=cast("Any", object())  # never used at construction
        )
    finally:
        get_settings.cache_clear()
    assert isinstance(service._sms_sender, LoggingSmsSender)
    assert isinstance(service._email_sender, LoggingEmailSender)


# --- bounded retry ladder (spec §25.4) ---------------------------------------------


async def test_bounded_retry_ladder_then_terminal_failure(
    db_engine: AsyncEngine,
) -> None:
    sms = FakeSmsSender()
    sms.fail_with(TemporaryProviderError("provider unavailable"), times=3)
    clock = StepClock(_T0)
    service = _service(db_engine, sms=sms, clock=clock)

    async with _seeded(db_engine) as seeded:
        delivery = seeded[2]

        first = await service.send(delivery.id, "req-retry-1")
        assert first.outcome is SendOutcome.RETRY_SCHEDULED
        row = await _row(db_engine, delivery.id)
        assert row.status == DeliveryStatus.RETRYABLE.value
        assert row.attempts == 1
        assert row.sent_at is None
        assert row.scheduled_at == _T0 + _ONE_MINUTE
        assert row.last_error is not None
        assert row.last_error.startswith("temporary:")

        clock.advance(_ONE_MINUTE)
        second = await service.send(delivery.id, "req-retry-2")
        assert second.outcome is SendOutcome.RETRY_SCHEDULED
        row = await _row(db_engine, delivery.id)
        assert row.status == DeliveryStatus.RETRYABLE.value
        assert row.attempts == 2
        assert row.scheduled_at == _T0 + _ONE_MINUTE + _FIVE_MINUTES

        clock.advance(_FIVE_MINUTES)
        third = await service.send(delivery.id, "req-retry-3")
        assert third.outcome is SendOutcome.FAILED
        row = await _row(db_engine, delivery.id)
        assert row.status == DeliveryStatus.FAILED.value
        assert row.attempts == 3
        assert row.sent_at is None
        assert row.provider_message_id is None
        assert row.last_error is not None
        assert row.last_error.startswith("temporary:")

        # Retries mutated the one row; no second delivery row ever.
        assert await _delivery_count(db_engine, delivery.event_key) == 1

        # Terminal stays terminal: a fourth arrival changes nothing.
        fourth = await service.send(delivery.id, "req-retry-4")
        assert fourth.outcome is SendOutcome.ALREADY_FAILED
        row = await _row(db_engine, delivery.id)
        assert row.attempts == 3

    assert sms.messages == []
    # The full ladder is pinned (spec §25.4: ~1m/~5m/~20m): the third
    # rung serves deployments that configure max_attempts deeper than
    # the default 3.
    assert RETRY_DELAYS == (_ONE_MINUTE, _FIVE_MINUTES, _TWENTY_MINUTES)


# --- lease clock domain (plan 07 final review I1) ----------------------------------


async def test_retryable_finalize_keeps_service_clock_updated_at(
    db_engine: AsyncEngine,
) -> None:
    # Regression, plan 07 final review I1: tx2 finalize re-assigns the
    # SAME `now` tx1 claimed with, so SQLAlchemy prunes the net-unchanged
    # updated_at column from the UPDATE. While the column carried an ORM
    # onupdate=func.now(), that pruning let the DB clock silently
    # overwrite the claim's service-clock lease stamp (mixed domains).
    # The frozen instant is deliberately YEARS away from wall time, so a
    # DB-clock write cannot pass the readback equality by coincidence.
    distant_t0 = datetime(2020, 1, 1, 0, 0, tzinfo=UTC)
    sms = FakeSmsSender()
    sms.fail_with(TemporaryProviderError("provider unavailable"), times=1)
    service = _service(db_engine, sms=sms, clock=FrozenClock(current=distant_t0))

    async with _seeded(db_engine, scheduled_at=distant_t0 - _ONE_MINUTE) as seeded:
        delivery = seeded[2]

        result = await service.send(delivery.id, "req-clock-1")
        assert result.outcome is SendOutcome.RETRY_SCHEDULED

        # Fresh session: the committed row, not a cached ORM instance.
        row = await _row(db_engine, delivery.id)
        assert row.status == DeliveryStatus.RETRYABLE.value
        # The lease stamp is the SERVICE-clock instant, both after the
        # tx1 claim AND after the tx2 finalize that re-assigned it.
        assert row.updated_at == distant_t0
        # Belt for the legible failure mode: nowhere near the DB clock
        # (which tracks wall time) either.
        assert abs((row.updated_at - datetime.now(UTC)).days) > 365

    assert sms.messages == []


# --- unknown outcome (timeout) ------------------------------------------------------


async def test_unknown_outcome_retries_same_row_same_key(
    db_engine: AsyncEngine,
) -> None:
    sms = FakeSmsSender()
    sms.fail_with(UnknownOutcomeError("gateway timeout"), times=1)
    clock = StepClock(_T0)
    service = _service(db_engine, sms=sms, clock=clock)

    async with _seeded(db_engine) as seeded:
        user, notification, delivery = seeded

        first = await service.send(delivery.id, "req-unknown-1")
        assert first.outcome is SendOutcome.RETRY_SCHEDULED
        row = await _row(db_engine, delivery.id)
        assert row.status == DeliveryStatus.RETRYABLE.value
        assert row.attempts == 1
        assert row.scheduled_at == _T0 + _ONE_MINUTE
        assert row.last_error is not None
        assert row.last_error.startswith("unknown_outcome:")

        clock.advance(_ONE_MINUTE)
        second = await service.send(delivery.id, "req-unknown-2")
        assert second.outcome is SendOutcome.SENT
        row = await _row(db_engine, delivery.id)
        assert row.status == DeliveryStatus.SENT.value
        assert row.attempts == 2
        assert row.sent_at == clock.now()
        assert row.last_error is None

        assert len(sms.messages) == 1
        assert sms.messages[0].idempotency_key == provider_idempotency_key(
            notification.event_key, NotificationChannel.SMS, user.id
        )
        # Never a second delivery row for a retry (spec §25.3).
        assert await _delivery_count(db_engine, notification.event_key) == 1


# --- permanent rejection --------------------------------------------------------------


async def test_permanent_rejection_fails_immediately(
    db_engine: AsyncEngine,
) -> None:
    sms = FakeSmsSender()
    sms.fail_with(PermanentProviderError("recipient blacklisted"), times=1)
    service = _service(db_engine, sms=sms, clock=StepClock(_T0))

    async with _seeded(db_engine) as seeded:
        delivery = seeded[2]

        result = await service.send(delivery.id, "req-perm-1")
        assert result.outcome is SendOutcome.FAILED
        row = await _row(db_engine, delivery.id)
        assert row.status == DeliveryStatus.FAILED.value
        assert row.attempts == 1
        assert row.sent_at is None
        assert row.last_error is not None
        assert row.last_error.startswith("permanent:")

        # No retry, no provider message, terminal on arrival.
        again = await service.send(delivery.id, "req-perm-2")
        assert again.outcome is SendOutcome.ALREADY_FAILED
        row = await _row(db_engine, delivery.id)
        assert row.attempts == 1

    assert sms.messages == []


# --- policy skips are not failures --------------------------------------


async def test_unverified_email_is_policy_skip_not_failure(
    db_engine: AsyncEngine,
) -> None:
    email = FakeEmailSender()
    service = _service(db_engine, email=email, clock=StepClock(_T0))

    async with _seeded(
        db_engine,
        user_overrides={
            "phone_e164": None,
            "email_normalized": "student@campus.example.edu",
            "email_verified_at": None,
        },
        event_type=NotificationEventType.SUBMISSION_APPROVED,
        event_key=f"submission:{uuid4()}:approved",
        channel=NotificationChannel.EMAIL,
    ) as seeded:
        delivery = seeded[2]

        result = await service.send(delivery.id, "req-skip-1")
        assert result.outcome is SendOutcome.SKIPPED
        assert result.skip_reason == "skipped:email_not_verified"
        row = await _row(db_engine, delivery.id)
        assert row.status == DeliveryStatus.SENT.value
        assert row.attempts == 1
        assert row.sent_at == _T0
        assert row.provider_message_id is None
        assert row.last_error == "skipped:email_not_verified"

    # The skip decision never reaches the provider port.
    assert email.messages == []


async def test_suppressed_claim_status_skips_deadline_reminder(
    db_engine: AsyncEngine,
) -> None:
    sms = FakeSmsSender()
    claim_id = uuid4()

    async def resolver(candidate: UUID) -> str | None:
        assert candidate == claim_id
        return "COMPLETED"

    service = _service(
        db_engine, sms=sms, clock=StepClock(_T0), claim_status_resolver=resolver
    )
    event_key = f"claim:{claim_id}:deadline_4h"

    async with _seeded(db_engine, event_key=event_key) as seeded:
        delivery = seeded[2]

        result = await service.send(delivery.id, "req-ddl-suppressed")
        assert result.outcome is SendOutcome.SKIPPED
        assert result.skip_reason == "skipped:deadline_claim_status:COMPLETED"
        row = await _row(db_engine, delivery.id)
        assert row.status == DeliveryStatus.SENT.value
        assert row.last_error == "skipped:deadline_claim_status:COMPLETED"
        assert row.sent_at == _T0

    assert sms.messages == []

    # An actionable claim does not suppress: the reminder goes out.
    actionable_claim_id = uuid4()

    async def actionable_resolver(candidate: UUID) -> str | None:
        assert candidate == actionable_claim_id
        return "CLAIMED"

    service = _service(
        db_engine,
        sms=sms,
        clock=StepClock(_T0),
        claim_status_resolver=actionable_resolver,
    )
    async with _seeded(
        db_engine, event_key=f"claim:{actionable_claim_id}:deadline_4h"
    ) as seeded:
        delivery = seeded[2]

        result = await service.send(delivery.id, "req-ddl-actionable")
        assert result.outcome is SendOutcome.SENT
        row = await _row(db_engine, delivery.id)
        assert row.status == DeliveryStatus.SENT.value

    assert len(sms.messages) == 1


# --- IN_APP --------------------------------------------------------------


async def test_in_app_delivery_uses_notification_row_as_inbox(
    db_engine: AsyncEngine,
) -> None:
    sms = FakeSmsSender()
    email = FakeEmailSender()
    service = _service(db_engine, sms=sms, email=email, clock=StepClock(_T0))

    async with _seeded(
        db_engine,
        user_overrides={"phone_e164": None},
        channel=NotificationChannel.IN_APP,
        notification_overrides={"read_at": _T0 - timedelta(hours=1)},
    ) as seeded:
        notification, delivery = seeded[1], seeded[2]

        result = await service.send(delivery.id, "req-inapp-1")
        assert result.outcome is SendOutcome.SENT
        row = await _row(db_engine, delivery.id)
        assert row.status == DeliveryStatus.SENT.value
        assert row.attempts == 1
        assert row.sent_at == _T0
        assert row.provider_message_id is None
        assert row.last_error is None
        # The message enters the inbox unread: completing the IN_APP
        # delivery clears any stale read marker.
        refreshed = await _notification_row(db_engine, notification.id)
        assert refreshed.read_at is None

    assert sms.messages == []
    assert email.messages == []


# --- claim-before-send race ----------------------------------------------


async def test_concurrent_claims_produce_single_provider_call(
    db_engine: AsyncEngine,
) -> None:
    sms = FakeSmsSender()
    # Two service instances = two independent sessions/connections, the
    # §7 concurrency-test shape; the row lock serializes the claims.
    service_a = _service(db_engine, sms=sms, clock=StepClock(_T0))
    service_b = _service(db_engine, sms=sms, clock=StepClock(_T0))

    async with _seeded(db_engine) as seeded:
        delivery = seeded[2]

        results = await asyncio.gather(
            service_a.send(delivery.id, "req-race-a"),
            service_b.send(delivery.id, "req-race-b"),
        )
        outcomes = [result.outcome for result in results]
        row = await _row(db_engine, delivery.id)
        assert row.status == DeliveryStatus.SENT.value
        assert row.attempts == 1
        assert len(sms.messages) == 1
        # Exactly one winner; the loser observed the claim (row still
        # SENDING) or the finished terminal state — never a second send.
        assert outcomes.count(SendOutcome.SENT) == 1
        loser = next(o for o in outcomes if o is not SendOutcome.SENT)
        assert loser in (SendOutcome.IN_FLIGHT, SendOutcome.ALREADY_SENT)
        assert await _delivery_count(db_engine, delivery.event_key) == 1


# --- stuck-SENDING lease heuristic (plan 07 T8) ---------------------------


async def test_stale_sending_claim_is_reclaimed(
    db_engine: AsyncEngine,
) -> None:
    # A SENDING row whose claim (updated_at) is older than the 15m
    # threshold is presumed dead: the next arrival re-claims it and the
    # delivery completes, attempts advancing on the re-claim.
    sms = FakeSmsSender()
    email = FakeEmailSender()
    service = _service(db_engine, sms=sms, email=email, clock=StepClock(_T0))

    async with _seeded(
        db_engine,
        user_overrides={"phone_e164": None},
        channel=NotificationChannel.IN_APP,
        delivery_overrides={
            "status": DeliveryStatus.SENDING.value,
            "attempts": 1,
            "updated_at": _T0 - _TWENTY_MINUTES,
        },
    ) as seeded:
        delivery = seeded[2]

        result = await service.send(delivery.id, "req-stale-1")
        assert result.outcome is SendOutcome.SENT
        row = await _row(db_engine, delivery.id)
        assert row.status == DeliveryStatus.SENT.value
        # 1 attempt died with the crashed sender; the re-claim is the 2nd.
        assert row.attempts == 2
        assert row.sent_at == _T0

    assert sms.messages == []
    assert email.messages == []


async def test_fresh_sending_claim_stays_in_flight(
    db_engine: AsyncEngine,
) -> None:
    # A SENDING claim younger than the threshold belongs to a live
    # sender: the row is observed IN_FLIGHT and nothing is written.
    sms = FakeSmsSender()
    email = FakeEmailSender()
    service = _service(db_engine, sms=sms, email=email, clock=StepClock(_T0))

    async with _seeded(
        db_engine,
        user_overrides={"phone_e164": None},
        channel=NotificationChannel.IN_APP,
        delivery_overrides={
            "status": DeliveryStatus.SENDING.value,
            "attempts": 1,
            "updated_at": _T0 - _FIVE_MINUTES,
        },
    ) as seeded:
        delivery = seeded[2]

        result = await service.send(delivery.id, "req-fresh-1")
        assert result.outcome is SendOutcome.IN_FLIGHT
        row = await _row(db_engine, delivery.id)
        assert row.status == DeliveryStatus.SENDING.value
        assert row.attempts == 1
        assert row.sent_at is None

    assert sms.messages == []
    assert email.messages == []


# --- not due -------------------------------------------------------------


async def test_not_due_delivery_is_left_pending(db_engine: AsyncEngine) -> None:
    sms = FakeSmsSender()
    service = _service(db_engine, sms=sms, clock=StepClock(_T0))

    async with _seeded(db_engine, scheduled_at=_T0 + timedelta(hours=1)) as seeded:
        delivery = seeded[2]

        result = await service.send(delivery.id, "req-future-1")
        assert result.outcome is SendOutcome.NOT_DUE
        row = await _row(db_engine, delivery.id)
        assert row.status == DeliveryStatus.PENDING.value
        assert row.attempts == 0
        assert row.sent_at is None

    assert sms.messages == []
