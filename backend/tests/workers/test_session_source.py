# backend/tests/workers/test_session_source.py
"""Pins for the shared per-job session source (hardening wave 1, P0-3).

The invariant under test: a Celery task body is a SYNC function driving
its coroutine with ``asyncio.run`` — one fresh, short-lived event loop
per task invocation — so a process-wide pooled ``AsyncEngine`` (and its
asyncpg connections) must never be implicitly reused across those
loops. The final-review reproduction saw consecutive task executions
fail intermittently with ``RuntimeError: Event loop is closed``; which
execution fails is a matter of timing, not a deterministic alternation,
which is exactly why the pattern survived per-job tests. The exception
sits in no ``autoretry_for`` list, and under ``task_acks_late=True``
Celery's default failure-ack semantics still acknowledge the message —
without a beat/watchdog the task instance never recovers.

Three guards:

- A live-DB regression test runs SEVERAL DISTINCT real job entries
  consecutively in ONE process, un-substituted (each task body composes
  through ``app.workers.session_source`` and owns a private engine that
  dies with its loop). Routing any of these jobs back at the shared
  pooled engine makes this test fail again.
- A static pin: no ``JOB_MODULES`` source may reference
  ``get_async_session_maker`` (that maker belongs to the FastAPI
  request scope, one long-lived loop), and the DB-composing jobs must
  import the shared session source.
- Fake-engine unit tests: one engine per call, disposed after the work
  returns, and the ``job_session_source`` shape matches what
  rebuild_rankings / validate_submission consume
  (``session_source()`` -> async context manager).
"""

from __future__ import annotations

import asyncio
import importlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from celery import Celery
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.notifications.enums import (
    DeliveryStatus,
    NotificationChannel,
    NotificationEventType,
)
from app.modules.notifications.models import Notification, NotificationDelivery
from app.modules.tasks.enums import (
    AssignmentAvailability,
    ClaimStatus,
    DeadlineMode,
    RewardLockStatus,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.models import Assignment, AssignmentClaim, Task
from app.workers.celery_app import JOB_MODULES, create_celery_app
from app.workers.jobs.dispatch_due_notifications import dispatch_due_notifications
from app.workers.jobs.expire_claims import expire_claim, expire_claims_scan
from app.workers.jobs.send_notification import send_notification_delivery
from app.workers.session_source import (
    job_session_source,
    run_with_session,
    run_with_session_maker,
)

# --- test database guard (mirrors tests/integration/db_guard.py) --------------------

_TEST_DATABASE_MARKER = "campusquest_test"
_DEFAULT_DATABASE_URL = (
    "postgresql+asyncpg://test:test@localhost:15432/campusquest_test"
)

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)

# The jobs this wave moved onto the shared session source; the pin below
# requires exactly these modules to compose through it.
_SHARED_SOURCE_JOBS = (
    "app.workers.jobs.dispatch_due_notifications",
    "app.workers.jobs.expire_claims",
    "app.workers.jobs.send_notification",
)


def _database_url() -> str:
    """The integration database URL, refusing non-test databases."""
    from sqlalchemy.engine import make_url

    url = os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)
    database = make_url(url).database or ""
    if _TEST_DATABASE_MARKER not in database:
        pytest.fail(
            f"Refusing session-source tests against non-test database "
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
def celery_app(monkeypatch: pytest.MonkeyPatch) -> Celery:
    """TEST-ONLY eager app (same pattern as test_celery_wiring.py):
    `.delay()` runs inline — the scans' per-id enqueues included — and no
    broker socket is ever opened."""
    _set_required_env(monkeypatch)
    app = create_celery_app(Settings())
    app.conf.task_always_eager = True
    return app


# --- live-DB regression: consecutive real job entries in one process -----------------


@pytest.mark.integration
def test_consecutive_real_job_entries_survive_per_task_event_loops(
    celery_app: Celery,
) -> None:
    """Wave-1 P0-3 regression, end to end and UN-substituted.

    One process executes, back to back: the expiry scan (which inline-
    runs ``expire_claim`` per discovery), the due-delivery scan (which
    inline-runs ``send_notification_delivery``), then direct replays of
    both per-id jobs, then both scans again — seven task executions,
    each ``asyncio.run`` on its own loop. Every task body composes
    through the shared per-job session source, so each execution owns a
    private engine created and disposed inside its own loop; nothing
    pooled crosses a loop boundary and every entry succeeds.

    Before the fix these bodies resolved the PROCESS-WIDE session maker,
    whose pooled engine was implicitly carried across the closed loops —
    consecutive executions then failed intermittently (timing, not a
    law) with ``RuntimeError: Event loop is closed``, an exception no
    autoretry list covers while ``task_acks_late`` still acknowledges
    the failed message. Re-routing any of these jobs at a shared pooled
    engine makes this test fail again."""
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    # Un-substituted real composition: the per-job engines come from the
    # shared session source against this same test DATABASE_URL. Clear
    # the settings cache around the run so those engines cannot resolve
    # a stale process-wide snapshot (pattern of
    # test_submission_validation_job.py).
    get_settings.cache_clear()

    run = uuid4().hex[:8]
    real_now = datetime.now(UTC)

    teacher = User(
        username=f"t{run}",
        password_hash=_PASSWORD_HASH,
        nickname="教师",
        phone_e164=None,
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
    )
    student = User(
        username=f"2025{run}001",
        password_hash=_PASSWORD_HASH,
        nickname="同学",
        phone_e164=None,
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )
    task_row = Task(
        owner_teacher_id=None,  # ids are flush-generated; set in seed()
        title="问卷回收",
        description="回收指定份数的问卷。",
        task_type=TaskType.DATA_CRAWL,
        rarity=TaskRarity.NORMAL,
        base_reward_points=100,
        status=TaskStatus.PUBLISHED,
        deadline_mode=DeadlineMode.RELATIVE,
        duration_minutes=4320,
        submission_schema={"columns": [{"name": "note", "type": "string"}]},
        submission_schema_version=2,
        allowed_file_types=["CSV"],
        max_file_size_bytes=200 * 1024 * 1024,
        notification_channels=["SMS"],
    )
    assignment = Assignment(
        task_id=None,
        platform="xiaohongshu",
        keyword="考研作息",
        availability_status=AssignmentAvailability.OCCUPIED,
    )
    claim = AssignmentClaim(
        assignment_id=None,
        task_id=None,
        user_id=None,
        status=ClaimStatus.CLAIMED,
        claimed_at=real_now - timedelta(days=5),
        deadline_at=real_now - timedelta(hours=2),
        grace_deadline_at=real_now - timedelta(hours=1),
        reward_policy_snapshot={"version": 1, "ladder_fractions": ["1", "0.8"]},
        base_reward_points_snapshot=100,
        submission_schema_version=1,
        reward_lock_status=RewardLockStatus.NONE,
    )
    notification = Notification(
        user_id=None,
        event_key=f"submission:{run}:approved",
        event_type=NotificationEventType.SUBMISSION_APPROVED.value,
        title="任务审核通过",
        body="您的提交已通过审核，获得100积分。",
    )
    delivery = NotificationDelivery(
        notification_id=None,
        user_id=None,
        event_key=notification.event_key,
        channel=NotificationChannel.IN_APP.value,
        status=DeliveryStatus.PENDING.value,
        scheduled_at=real_now - timedelta(minutes=1),
        attempts=0,
    )

    async def seed() -> None:
        """Parents flush before the children that denormalize their ids
        (same ordering as the other worker end-to-end tests)."""
        async with maker() as session:
            session.add_all([teacher, student])
            await session.flush()
            task_row.owner_teacher_id = teacher.id
            session.add(task_row)
            await session.flush()
            assignment.task_id = task_row.id
            session.add(assignment)
            await session.flush()
            claim.assignment_id = assignment.id
            claim.task_id = task_row.id
            claim.user_id = student.id
            session.add(claim)
            notification.user_id = student.id
            session.add(notification)
            await session.flush()
            delivery.notification_id = notification.id
            delivery.user_id = student.id
            delivery.event_key = notification.event_key
            session.add(delivery)
            await session.commit()

    async def cleanup() -> None:
        """FK-ordered teardown; real commits leave no rollback to trust."""
        async with maker() as session:
            await session.execute(
                delete(NotificationDelivery).where(
                    NotificationDelivery.id == delivery.id
                )
            )
            await session.execute(
                delete(Notification).where(Notification.id == notification.id)
            )
            await session.execute(
                delete(AssignmentClaim).where(AssignmentClaim.id == claim.id)
            )
            await session.execute(
                delete(Assignment).where(Assignment.id == assignment.id)
            )
            await session.execute(delete(Task).where(Task.id == task_row.id))
            await session.execute(
                delete(User).where(User.id.in_([teacher.id, student.id]))
            )
            await session.commit()

    asyncio.run(seed())
    try:
        # 1. Expiry chain: the scan discovers the due claim and the
        #    inline per-id job expires it (eager: same process, its own
        #    asyncio.run inside the scan's delay call).
        scan_one = expire_claims_scan.delay("req-loop-1").get(timeout=30)
        assert scan_one["discovered"] == 1
        assert scan_one["claim_ids"] == [str(claim.id)]

        # 2. Notification chain: the scan discovers the due IN_APP
        #    delivery and the inline send job resolves it SENT.
        dispatch_one = dispatch_due_notifications.delay("req-loop-2").get(timeout=30)
        assert dispatch_one["enqueued"] == 1
        assert dispatch_one["due"] == 1
        assert dispatch_one["delivery_ids"] == [str(delivery.id)]

        # 3. Direct per-id replays (at-least-once redelivery shape):
        #    idempotent no-ops, not errors.
        expire_replay = expire_claim.delay(str(claim.id), "req-loop-3").get(timeout=30)
        assert expire_replay["outcome"] == "ALREADY_TERMINAL"

        send_replay = send_notification_delivery.delay(
            str(delivery.id), "req-loop-4"
        ).get(timeout=30)
        assert send_replay["outcome"] == "ALREADY_SENT"

        # 4. Both scans again on yet more fresh loops: nothing left to
        #    discover (terminal rows left both candidate sets).
        scan_two = expire_claims_scan.delay("req-loop-5").get(timeout=30)
        assert scan_two["discovered"] == 0
        dispatch_two = dispatch_due_notifications.delay("req-loop-6").get(timeout=30)
        assert dispatch_two["enqueued"] == 0
    finally:
        asyncio.run(cleanup())
        get_settings.cache_clear()
    asyncio.run(engine.dispose())


# --- static pins: the shared source is the only legal DB composition -----------------


def test_no_job_module_references_the_process_wide_session_maker() -> None:
    """``get_async_session_maker`` binds sessions to the process-wide
    pooled engine — legal only on the FastAPI request scope's one
    long-lived loop. Inside a Celery body (an ``asyncio.run`` per task
    invocation) those pooled connections are implicitly reused across
    closed loops, the P0-3 failure this wave fixed. A job that ever
    genuinely runs on one persistent loop would need to revisit this
    pin deliberately; none exists today."""
    offenders: dict[str, int] = {}
    for module_name in JOB_MODULES:
        module = importlib.import_module(module_name)
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        count = source.count("get_async_session_maker")
        if count:
            offenders[module_name] = count
    assert not offenders, (
        f"JOB_MODULES sources must not reference get_async_session_maker "
        f"(the FastAPI-scope process-wide pooled engine): {offenders}. "
        f"Compose through app.workers.session_source instead — one fresh "
        f"engine per task invocation inside the job's own asyncio.run."
    )


def test_db_jobs_compose_through_the_shared_session_source() -> None:
    """The jobs this wave moved must import the shared source, so a
    future edit cannot quietly fall back to a private engine recipe.
    rebuild_rankings / validate_submission keep their own equivalent
    per-job copies (left untouched this wave to avoid conflicting with
    the parallel stream); the pin above still bans the process-wide
    maker for them."""
    missing: list[str] = []
    for module_name in _SHARED_SOURCE_JOBS:
        module = importlib.import_module(module_name)
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        if "from app.workers.session_source import" not in source:
            missing.append(module_name)
    assert not missing, (
        f"these job modules must compose through app.workers.session_source: {missing}"
    )


# --- lifecycle unit tests (fake engines; no database) -------------------------------


class _FakeEngine:
    """Only the lifecycle surface the session source touches: the work
    never opens a connection, so ``dispose`` is the sole observable
    call."""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def dispose(self) -> None:
        self._events.append("dispose")


class _FakeSessionContext:
    """The ``async with maker() as session`` shape over a fake engine."""

    def __init__(self, engine: _FakeEngine) -> None:
        self.engine = engine

    async def __aenter__(self) -> _FakeSessionContext:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _EngineCalls:
    """Records every engine the source builds and every maker it binds.

    ``create_db_engine`` and ``async_sessionmaker`` are both resolved
    lazily at call time inside app.workers.session_source, so patching
    the module attributes routes the REAL source code onto these fakes
    (the same seam the jobs' production path uses).
    """

    def __init__(self) -> None:
        self.events: list[str] = []
        self.engines: list[_FakeEngine] = []
        self.maker_engines: list[_FakeEngine] = []
        self.maker_kwargs: list[dict[str, Any]] = []

    def create_db_engine(self, settings: object) -> _FakeEngine:
        engine = _FakeEngine(self.events)
        self.engines.append(engine)
        return engine

    def async_sessionmaker(self, engine: Any, **kwargs: Any) -> Any:
        self.maker_engines.append(engine)
        self.maker_kwargs.append(kwargs)

        def _maker() -> _FakeSessionContext:
            return _FakeSessionContext(engine)

        return _maker


@pytest.fixture
def engine_calls(monkeypatch: pytest.MonkeyPatch) -> _EngineCalls:
    """Route the session source onto recording fakes with test env set
    (``get_settings`` needs the required deployment variables)."""
    _set_required_env(monkeypatch)
    calls = _EngineCalls()
    monkeypatch.setattr("app.db.session.create_db_engine", calls.create_db_engine)
    monkeypatch.setattr(
        "sqlalchemy.ext.asyncio.async_sessionmaker", calls.async_sessionmaker
    )
    return calls


def test_run_with_session_uses_one_disposed_engine_per_call(
    engine_calls: _EngineCalls,
) -> None:
    async def work(session: Any) -> int:
        engine_calls.events.append("work")
        return 42

    settings = Settings()
    assert asyncio.run(run_with_session(work, settings=settings)) == 42
    assert asyncio.run(run_with_session(work, settings=settings)) == 42

    # One engine per call — each bound into its own maker (with the
    # verified jobs' expire_on_commit=False semantics) and disposed
    # after its session closes. Never shared, never leaked.
    assert len(engine_calls.engines) == 2
    assert engine_calls.maker_engines == engine_calls.engines
    assert all(
        kwargs == {"expire_on_commit": False} for kwargs in engine_calls.maker_kwargs
    )
    assert engine_calls.events == ["work", "dispose", "work", "dispose"]


def test_run_with_session_maker_disposes_after_work_returns(
    engine_calls: _EngineCalls,
) -> None:
    makers: list[Any] = []

    async def work(session_maker: Any) -> str:
        makers.append(session_maker)
        engine_calls.events.append("work")
        return "ok"

    assert asyncio.run(run_with_session_maker(work, settings=Settings())) == "ok"

    # The maker's engine outlives the work (the collaborator's sessions
    # close first) and is disposed right after, inside the same loop.
    assert len(engine_calls.engines) == 1
    assert engine_calls.maker_engines == engine_calls.engines
    assert engine_calls.events == ["work", "dispose"]
    assert len(makers) == 1


def test_job_session_source_matches_the_injected_factory_shape(
    engine_calls: _EngineCalls,
) -> None:
    """The shape rebuild_rankings / validate_submission consume:
    ``source = job_session_source(...)`` is a CALLABLE whose call
    returns the async context manager yielding the session — never the
    context-manager instance itself (that production-only path once
    crashed while every test injected fakes; see
    test_submission_validation_job.py)."""
    source = job_session_source(Settings())
    assert callable(source)

    async def _two_sessions() -> list[Any]:
        sessions = []
        async with source() as first:
            sessions.append(first)
        async with source() as second:
            sessions.append(second)
        return sessions

    sessions = asyncio.run(_two_sessions())
    assert len(engine_calls.engines) == 2
    # Each yielded session context sits on its OWN engine, disposed
    # when the context exits.
    assert [session.engine for session in sessions] == engine_calls.engines
    assert engine_calls.events == ["dispose", "dispose"]


def test_job_session_source_settings_none_falls_back_to_get_settings(
    engine_calls: _EngineCalls, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settings parameter exists so callers that already hold a
    Settings snapshot (the shell pattern) share one resolution path;
    ``None`` falls back to exactly one ``get_settings()`` per
    invocation (the live-DB tests above exercise that default for
    real)."""
    _set_required_env(monkeypatch)
    seen: list[Settings] = []
    original = get_settings

    def _recording_get_settings() -> Settings:
        settings = original()
        seen.append(settings)
        return settings

    monkeypatch.setattr("app.core.config.get_settings", _recording_get_settings)

    async def work(session: Any) -> None:
        return None

    # Explicit settings: no ambient resolution at all.
    asyncio.run(run_with_session(work, settings=Settings()))
    assert seen == []

    # None: exactly one get_settings() per invocation.
    asyncio.run(run_with_session(work))
    assert len(seen) == 1
