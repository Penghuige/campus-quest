# backend/tests/integration/submissions/test_submission_api.py
"""The submission/review HTTP API end-to-end over real PostgreSQL + Redis
(spec §10 steps 1-8, §11, §12.4, §28 URL shapes, §29 envelope, §33.1 rate
limit, §33.3 short-lived download links, §40 no object keys; plan 04 task
10; backend-engineering §3/§16).

Drives the real app (``create_app()`` — real routes, real envelope
handlers, the submissions router mounted under /api/v1) through the
surfaces the task brief freezes:

- the full student flow: upload intent (server-keyed presigned URL, no
  object-key field), fake object PUT, upload-complete (Submission
  created + the validation job ENQUEUED through the injected dispatcher
  fake carrying the request's X-Request-ID), the worker stand-in
  (ValidationService + RewardLockService on the harness session), then
  the validation report readable by the owner with the §12.4 shape and
  scan assertions that no object key or parser internal leaks;
- ownership: another Student is refused (403) on every step — intent,
  complete, validation read, download;
- downloads: the short-lived URL is minted only after authorization
  (owner, task owner, REVIEW_SUBMISSIONS collaborator; never an
  unrelated teacher or another student) and the response carries the
  URL, never the key;
- the teacher review queue: VALIDATED-but-undecided submissions on
  own/collaborating tasks only, oldest first, offset-paginated, with
  claim/task context (platform/keyword), the locked reward tier, the
  validation summary + preview, and the API download path;
- the three review actions through the API: approve (claim COMPLETED,
  one grant through the fake points port, idempotent replay answers
  already_reviewed), revision-required (note mandatory at the
  transport), invalidate-reward-lock (reason mandatory — blank answers
  the typed 400), and the permission matrix on them;
- rate limiting on upload-intent normalized to the authenticated user
  id (429 envelope on an exhausted window).

Seams are dependency overrides, not route fakes: the rollback-harness
session, the FakeObjectStorage (client PUT + worker-side read), the
FakeValidationDispatcher capturing enqueues (NOT Celery eager mode — no
broker is contacted), the FakeRateLimiter, the InMemoryEventCollector,
and the FakePointsRewardPort.

One sync test exercises the REAL job body (``run_submission_validation``)
with the same DI discipline on committed NullPool sessions: it proves
the worker-side chaining — a VALIDATED run immediately calls
``RewardLockService.on_validation_passed`` (claim UNDER_REVIEW +
PROVISIONAL lock + the REWARD_LOCKED audit event), idempotently on
replay.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from fastapi import FastAPI
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import get_db_session
from app.main import create_app
from app.modules.identity.dependencies import (
    get_access_token_codec,
    get_business_clock,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import InMemoryEventCollector
from app.modules.identity.models import TotpCredential, User
from app.modules.identity.session_service import SessionService
from app.modules.submissions.models import (
    RewardLockHistory,
    Submission,
    SubmissionReview,
    SubmissionValidation,
)
from app.modules.submissions.router import (
    get_event_publisher,
    get_object_storage,
    get_points_port,
    get_rate_limiter,
    get_submissions_redis,
    get_validation_dispatcher,
)
from app.modules.tasks.enums import (
    AssignmentAvailability,
    ClaimStatus,
    DeadlineMode,
    RewardLockStatus,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.models import Assignment, AssignmentClaim, Task, TaskCollaborator
from tests.fakes.integrations import FakeObjectStorage, FakeRateLimiter
from tests.fakes.points import FakePointsRewardPort

_SUBMISSIONS_TEST_REDIS_DB = 15
# Anchored to the real now: PyJWT validates `exp` at decode time against
# wall-clock time, so tokens minted by the app must be "just now".
_T0 = datetime.now(UTC).replace(microsecond=0)
_PASSWORD = "correct-horse-battery"

_OWNER_EMAIL = "submission-owner@pku.edu.cn"
_OTHER_OWNER_EMAIL = "submission-other-owner@pku.edu.cn"
_REVIEW_COLLEAGUE_EMAIL = "submission-reviewer@pku.edu.cn"
_VIEW_COLLEAGUE_EMAIL = "submission-viewer@pku.edu.cn"
_OUTSIDER_EMAIL = "submission-outsider@pku.edu.cn"
_STUDENT_NUMBER = "20250021001"
_OTHER_STUDENT_NUMBER = "20250021002"

_CSV_BYTES = (
    b"url,title\n"
    b"https://example.com/note/1,\xe7\xac\xac\xe4\xb8\x80\xe6\x9d\xa1\n"
    b"https://example.com/note/2,\xe7\xac\xac\xe4\xba\x8c\xe6\x9d\xa1\n"
)

_CSV_SCHEMA = {
    "required_columns": [
        {"name": "url", "type": "string", "unique": True},
        {"name": "title", "type": "string"},
    ]
}

_INTENT_FIELDS = {"intent_id", "upload_url", "expires_at", "headers"}
_SUBMISSION_FIELDS = {
    "id",
    "claim_id",
    "version",
    "original_filename",
    "declared_type",
    "file_size",
    "submitted_at",
    "validation_status",
    "review_status",
    "created_at",
}
_VALIDATION_FIELDS = {
    "submission_id",
    "claim_id",
    "version",
    "validation_status",
    "review_status",
    "detected_type",
    "report",
}
_REPORT_FIELDS = {
    "parser_version",
    "file_type",
    "row_count",
    "detected_columns",
    "missing_required_columns",
    "extra_columns",
    "type_error_counts",
    "null_ratios",
    "duplicate_counts",
    "warnings",
    "errors",
    "duration_ms",
    "preview_rows",
}
_DOWNLOAD_FIELDS = {"url", "expires_at"}
_QUEUE_FIELDS = {"items", "total", "limit", "offset"}
_QUEUE_ITEM_FIELDS = {
    "submission_id",
    "claim_id",
    "task_id",
    "task_title",
    "platform",
    "keyword",
    "version",
    "original_filename",
    "declared_type",
    "detected_type",
    "file_size",
    "submitted_at",
    "review_status",
    "claim_status",
    "reward_tier_locked",
    "locked_reward_points",
    "validation",
    "download_url",
}
_APPROVE_FIELDS = {
    "claim_id",
    "claim_status",
    "reward_lock_status",
    "points_granted",
    "already_reviewed",
}
_REVISION_FIELDS = {
    "claim_id",
    "claim_status",
    "reward_lock_status",
    "revision_deadline_at",
}


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail(f"API integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_SUBMISSIONS_TEST_REDIS_DB}"))


# --- fixtures -----------------------------------------------------------------------


@pytest_asyncio.fixture
async def api_redis() -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(_test_redis_url(), decode_responses=True)
    try:
        await client.flushdb()
        yield client
        await client.flushdb()
    finally:
        await client.aclose()


@pytest.fixture
def api_clock() -> FrozenClock:
    return FrozenClock(_T0)


@pytest.fixture
def fake_limiter() -> FakeRateLimiter:
    return FakeRateLimiter()


@pytest.fixture
def event_collector() -> InMemoryEventCollector:
    return InMemoryEventCollector()


@pytest.fixture
def fake_points() -> FakePointsRewardPort:
    return FakePointsRewardPort()


@dataclass
class CapturedDispatch:
    """One recorded enqueue, exactly as invoked."""

    submission_id: UUID
    request_id: str


class FakeValidationDispatcher:
    """In-memory ValidationDispatcher recording every enqueue (the API
    test seam — NOT Celery eager mode: no broker, no inline execution)."""

    def __init__(self) -> None:
        self.calls: list[CapturedDispatch] = []

    def enqueue_validation(self, submission_id: UUID, request_id: str) -> None:
        self.calls.append(
            CapturedDispatch(submission_id=submission_id, request_id=request_id)
        )


@pytest.fixture
def fake_dispatcher() -> FakeValidationDispatcher:
    return FakeValidationDispatcher()


@pytest.fixture
def fake_storage(api_clock: FrozenClock) -> FakeObjectStorage:
    return FakeObjectStorage(clock=api_clock)


@pytest.fixture
def api_app(
    db_session: AsyncSession,
    api_redis: aioredis.Redis,
    api_clock: FrozenClock,
    fake_limiter: FakeRateLimiter,
    event_collector: InMemoryEventCollector,
    fake_points: FakePointsRewardPort,
    fake_storage: FakeObjectStorage,
    fake_dispatcher: FakeValidationDispatcher,
) -> FastAPI:
    app = create_app()

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _test_db_session
    app.dependency_overrides[get_business_clock] = lambda: api_clock
    app.dependency_overrides[get_submissions_redis] = lambda: api_redis
    app.dependency_overrides[get_rate_limiter] = lambda: fake_limiter
    app.dependency_overrides[get_object_storage] = lambda: fake_storage
    app.dependency_overrides[get_validation_dispatcher] = lambda: fake_dispatcher
    app.dependency_overrides[get_event_publisher] = lambda: event_collector
    app.dependency_overrides[get_points_port] = lambda: fake_points
    return app


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


# --- seeding helpers ----------------------------------------------------------------


async def _seed_user(db: AsyncSession, *, username: str, role: Role) -> User:
    user = User(
        username=username,
        password_hash=hash_password(_PASSWORD),
        nickname="测试用户",
        role=role.value,
        status=UserStatus.ACTIVE.value,
    )
    db.add(user)
    await db.flush()
    return user


async def _session_tokens(db: AsyncSession, clock: FrozenClock, user: User) -> dict:
    sessions = SessionService(clock=clock, access_codec=get_access_token_codec())
    _, tokens = await sessions.issue_session(db, user=user, now=clock.now())
    return {"access_token": tokens.access_token}


def _bearer(tokens: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _seed_management_teacher(
    db: AsyncSession, clock: FrozenClock, *, username: str
) -> tuple[User, dict]:
    """A TEACHER able to pass ``require_staff_management_actor`` (role +
    ACTIVE + a confirmed TOTP credential row)."""
    user = await _seed_user(db, username=username, role=Role.TEACHER)
    db.add(
        TotpCredential(
            user_id=user.id,
            secret_encrypted=b"test-stand-in-secret",
            confirmed_at=clock.now(),
        )
    )
    await db.flush()
    return user, await _session_tokens(db, clock, user)


def _seed_task(owner_id: Any, **overrides: Any) -> Task:
    fields: dict[str, Any] = {
        "owner_teacher_id": owner_id,
        "title": "小红书考研经验帖数据采集",
        "description": "采集指定关键词下的笔记正文与互动数据。",
        "task_type": TaskType.DATA_CRAWL.value,
        "rarity": TaskRarity.NORMAL.value,
        "base_reward_points": 100,
        "status": TaskStatus.PUBLISHED.value,
        "deadline_mode": DeadlineMode.RELATIVE.value,
        "duration_minutes": 4320,
        "allowed_file_types": ["CSV"],
        "max_file_size_bytes": 10 * 1024 * 1024,
        "notification_channels": ["SMS"],
        "published_at": _T0 - timedelta(days=1),
        "submission_schema": _CSV_SCHEMA,
        "submission_schema_version": 1,
    }
    fields.update(overrides)
    return Task(**fields)


def _seed_assignment(task_id: Any, keyword: str) -> Assignment:
    return Assignment(
        task_id=task_id,
        platform="xiaohongshu",
        keyword=keyword,
        availability_status=AssignmentAvailability.OCCUPIED.value,
    )


def _seed_claim(
    task: Task, assignment: Assignment, student_id: Any, **overrides: Any
) -> AssignmentClaim:
    fields: dict[str, Any] = {
        "assignment_id": assignment.id,
        "task_id": task.id,
        "user_id": student_id,
        "status": ClaimStatus.CLAIMED.value,
        "claimed_at": _T0 - timedelta(hours=1),
        "deadline_at": _T0 + timedelta(days=3),
        "grace_deadline_at": _T0 + timedelta(days=4),
        "reward_policy_snapshot": {"version": 1},
        "base_reward_points_snapshot": 100,
        "submission_schema_version": 1,
        "reward_lock_status": RewardLockStatus.NONE.value,
    }
    fields.update(overrides)
    return AssignmentClaim(**fields)


def _report_json(
    *, row_count: int, preview_rows: list[list[str]] | None = None
) -> dict[str, Any]:
    """A minimal persisted §12.4 report (queue-seeded submissions)."""
    return {
        "parser_version": "csv-1",
        "file_type": "CSV",
        "row_count": row_count,
        "detected_columns": ["url", "title"],
        "missing_required_columns": [],
        "extra_columns": [],
        "type_error_counts": {},
        "null_ratios": {"url": 0.0, "title": 0.0},
        "duplicate_counts": {"url": 0},
        "warnings": [],
        "errors": [],
        "duration_ms": 12.5,
        "preview_rows": preview_rows if preview_rows is not None else [],
    }


def _seed_submission(
    claim: AssignmentClaim,
    *,
    version: int = 1,
    submitted_at: datetime | None = None,
    validation_status: str = "VALIDATED",
    review_status: str = "PENDING_REVIEW",
    report: dict[str, Any] | None = None,
) -> Submission:
    when = submitted_at if submitted_at is not None else _T0
    return Submission(
        claim_id=claim.id,
        version=version,
        object_key=f"submissions/{claim.id}/{uuid4()}",
        original_filename=f"数据v{version}.csv",
        declared_type="CSV",
        file_size=len(_CSV_BYTES),
        submitted_at=when,
        validation_status=validation_status,
        review_status=review_status,
        validation_report=report if report is not None else _report_json(row_count=2),
        detected_type="CSV" if validation_status == "VALIDATED" else None,
        retention_until=when + timedelta(days=180),
    )


async def _run_worker_stand_in(
    db: AsyncSession,
    storage: FakeObjectStorage,
    clock: FrozenClock,
    events: InMemoryEventCollector,
    submission_id: UUID,
) -> None:
    """The validation worker stand-in: the real ValidationService +
    RewardLockService the job chains, on the harness session."""
    from app.modules.submissions.reward_lock_service import RewardLockService
    from app.modules.submissions.validation_runner import (
        SandboxLimits,
        ValidatorSandbox,
    )
    from app.modules.submissions.validation_service import ValidationService
    from app.modules.submissions.validators.common import PreviewSpec

    validation = ValidationService(
        clock=clock,
        storage=storage,
        sandbox=ValidatorSandbox(
            limits=SandboxLimits(
                wall_timeout_seconds=60.0,
                memory_limit_bytes=1024 * 1024 * 1024,
                cpu_seconds=60,
            )
        ),
        preview=PreviewSpec(max_rows=10, max_value_length=200),
    )
    await validation.validate_submission(db, submission_id)
    locks = RewardLockService(clock=clock, events=events)
    await locks.on_validation_passed(db, submission_id)


def _envelope(response: httpx.Response) -> dict:
    body = response.json()
    assert set(body) == {"error"}, body
    error = body["error"]
    assert set(error) == {"code", "message", "details", "request_id"}, error
    assert error["request_id"] == response.headers["X-Request-ID"]
    return error


# --- the student flow (brief step 1) --------------------------------------------------


@pytest.mark.integration
async def test_full_submission_flow_enqueues_validation_and_reports_back(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
    fake_storage: FakeObjectStorage,
    fake_dispatcher: FakeValidationDispatcher,
    event_collector: InMemoryEventCollector,
) -> None:
    """Owner requests the upload -> fake object PUT -> upload-complete
    (enqueue captured with the request id) -> the worker stand-in
    validates + locks -> the owner reads the report. No object key or
    parser internal ever appears in a student response."""
    owner, _ = await _seed_management_teacher(
        db_session, api_clock, username=_OWNER_EMAIL
    )
    student = await _seed_user(db_session, username=_STUDENT_NUMBER, role=Role.STUDENT)
    student_tokens = await _session_tokens(db_session, api_clock, student)
    task = _seed_task(owner.id)
    db_session.add(task)
    await db_session.flush()
    assignment = _seed_assignment(task.id, "考研经验")
    db_session.add(assignment)
    await db_session.flush()
    claim = _seed_claim(task, assignment, student.id)
    db_session.add(claim)
    await db_session.flush()

    # -- the owner requests the presigned upload ------------------------------
    intended = await client.post(
        "/api/v1/submissions/upload-intent",
        json={
            "claim_id": str(claim.id),
            "filename": "../../考研数据.csv",
            "declared_type": "CSV",
            "size": len(_CSV_BYTES),
        },
        headers=_bearer(student_tokens),
    )
    assert intended.status_code == 201, intended.text
    intent = intended.json()
    assert set(intent) == _INTENT_FIELDS
    assert "object_key" not in intent
    object_key = fake_storage.upload_urls[-1].object_key

    # -- the browser completes the presigned PUT directly to storage ----------
    fake_storage.put_object(object_key=object_key, content=_CSV_BYTES)

    # -- upload-complete: Submission + enqueued validation job ----------------
    completed = await client.post(
        "/api/v1/submissions/upload-complete",
        json={"intent_id": intent["intent_id"]},
        headers={**_bearer(student_tokens), "X-Request-ID": "req-api-flow-1"},
    )
    assert completed.status_code == 200, completed.text
    submission = completed.json()
    assert set(submission) == _SUBMISSION_FIELDS
    assert submission["claim_id"] == str(claim.id)
    assert submission["version"] == 1
    # The sanitized display filename never carries path material (spec §10).
    assert submission["original_filename"] == "考研数据.csv"
    assert submission["declared_type"] == "CSV"
    assert submission["file_size"] == len(_CSV_BYTES)
    assert submission["validation_status"] == "UPLOADED"
    assert submission["review_status"] == "PENDING_REVIEW"
    assert "object_key" not in submission
    assert object_key not in completed.text

    submission_id = submission["id"]
    assert fake_dispatcher.calls == [
        CapturedDispatch(submission_id=UUID(submission_id), request_id="req-api-flow-1")
    ]

    # -- the worker stand-in validates + locks --------------------------------
    await _run_worker_stand_in(
        db_session, fake_storage, api_clock, event_collector, UUID(submission_id)
    )

    # -- the owner reads the validation report --------------------------------
    report = await client.get(
        f"/api/v1/submissions/{submission_id}/validation",
        headers=_bearer(student_tokens),
    )
    assert report.status_code == 200, report.text
    body = report.json()
    assert set(body) == _VALIDATION_FIELDS
    assert body["submission_id"] == submission_id
    assert body["validation_status"] == "VALIDATED"
    assert body["detected_type"] == "CSV"
    assert set(body["report"]) == _REPORT_FIELDS
    assert body["report"]["row_count"] == 2
    assert body["report"]["errors"] == []
    assert body["report"]["preview_rows"] == [
        ["https://example.com/note/1", "第一条"],
        ["https://example.com/note/2", "第二条"],
    ]
    # Privacy scans (spec §40): no object key, no storage path material.
    assert "object_key" not in body
    assert object_key not in report.text
    assert "submissions/" not in report.text

    # The replay of upload-complete after the pipeline moved on returns the
    # SAME Submission and does NOT enqueue a second job.
    replayed = await client.post(
        "/api/v1/submissions/upload-complete",
        json={"intent_id": intent["intent_id"]},
        headers=_bearer(student_tokens),
    )
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["id"] == submission_id
    assert len(fake_dispatcher.calls) == 1

    # The claim moved into the review pipeline (worker-side chaining).
    await db_session.refresh(claim)
    assert claim.status == ClaimStatus.UNDER_REVIEW.value
    assert claim.reward_lock_status == RewardLockStatus.PROVISIONAL.value
    assert claim.reward_tier_locked == 100


@pytest.mark.integration
async def test_another_student_is_denied_every_step(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
    fake_storage: FakeObjectStorage,
    fake_dispatcher: FakeValidationDispatcher,
    event_collector: InMemoryEventCollector,
) -> None:
    """A different Student receives PERMISSION_DENIED on the intent, the
    finalize, the validation read, and the download — every resource the
    first student created."""
    owner, _ = await _seed_management_teacher(
        db_session, api_clock, username=_OWNER_EMAIL
    )
    student = await _seed_user(db_session, username=_STUDENT_NUMBER, role=Role.STUDENT)
    student_tokens = await _session_tokens(db_session, api_clock, student)
    other = await _seed_user(
        db_session, username=_OTHER_STUDENT_NUMBER, role=Role.STUDENT
    )
    other_tokens = await _session_tokens(db_session, api_clock, other)
    task = _seed_task(owner.id)
    db_session.add(task)
    await db_session.flush()
    assignment = _seed_assignment(task.id, "考研经验")
    db_session.add(assignment)
    await db_session.flush()
    claim = _seed_claim(task, assignment, student.id)
    db_session.add(claim)
    await db_session.flush()

    intended = await client.post(
        "/api/v1/submissions/upload-intent",
        json={
            "claim_id": str(claim.id),
            "filename": "数据.csv",
            "declared_type": "CSV",
            "size": len(_CSV_BYTES),
        },
        headers=_bearer(other_tokens),
    )
    assert intended.status_code == 403, intended.text
    assert _envelope(intended)["code"] == "PERMISSION_DENIED"

    # The OWNER completes a real submission for the later steps.
    intended = await client.post(
        "/api/v1/submissions/upload-intent",
        json={
            "claim_id": str(claim.id),
            "filename": "数据.csv",
            "declared_type": "CSV",
            "size": len(_CSV_BYTES),
        },
        headers=_bearer(student_tokens),
    )
    assert intended.status_code == 201, intended.text
    intent = intended.json()
    fake_storage.put_object(
        object_key=fake_storage.upload_urls[-1].object_key, content=_CSV_BYTES
    )
    completed = await client.post(
        "/api/v1/submissions/upload-complete",
        json={"intent_id": intent["intent_id"]},
        headers=_bearer(student_tokens),
    )
    assert completed.status_code == 200, completed.text
    submission_id = completed.json()["id"]

    denied_complete = await client.post(
        "/api/v1/submissions/upload-complete",
        json={"intent_id": intent["intent_id"]},
        headers=_bearer(other_tokens),
    )
    assert denied_complete.status_code == 403, denied_complete.text
    assert _envelope(denied_complete)["code"] == "PERMISSION_DENIED"

    denied_report = await client.get(
        f"/api/v1/submissions/{submission_id}/validation",
        headers=_bearer(other_tokens),
    )
    assert denied_report.status_code == 403, denied_report.text
    assert _envelope(denied_report)["code"] == "PERMISSION_DENIED"

    denied_download = await client.get(
        f"/api/v1/submissions/{submission_id}/download",
        headers=_bearer(other_tokens),
    )
    assert denied_download.status_code == 403, denied_download.text
    assert _envelope(denied_download)["code"] == "PERMISSION_DENIED"

    # Nothing was enqueued or written by the denied attempts.
    assert len(fake_dispatcher.calls) == 1


# --- downloads (spec §33.3) -----------------------------------------------------------


@pytest.mark.integration
async def test_download_url_issued_only_after_authorization(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
    fake_storage: FakeObjectStorage,
    fake_dispatcher: FakeValidationDispatcher,
    event_collector: InMemoryEventCollector,
) -> None:
    """The short-lived download URL is minted per request, only for the
    submission owner, the task owner, and a REVIEW_SUBMISSIONS
    collaborator — an unrelated teacher and another student are refused;
    the response carries the URL, never the key."""
    owner, owner_tokens = await _seed_management_teacher(
        db_session, api_clock, username=_OWNER_EMAIL
    )
    colleague, colleague_tokens = await _seed_management_teacher(
        db_session, api_clock, username=_REVIEW_COLLEAGUE_EMAIL
    )
    outsider, outsider_tokens = await _seed_management_teacher(
        db_session, api_clock, username=_OUTSIDER_EMAIL
    )
    student = await _seed_user(db_session, username=_STUDENT_NUMBER, role=Role.STUDENT)
    student_tokens = await _session_tokens(db_session, api_clock, student)
    other = await _seed_user(
        db_session, username=_OTHER_STUDENT_NUMBER, role=Role.STUDENT
    )
    other_tokens = await _session_tokens(db_session, api_clock, other)
    task = _seed_task(owner.id)
    db_session.add(task)
    await db_session.flush()
    db_session.add(
        TaskCollaborator(
            task_id=task.id,
            teacher_id=colleague.id,
            permissions=["REVIEW_SUBMISSIONS"],
        )
    )
    assignment = _seed_assignment(task.id, "考研经验")
    db_session.add(assignment)
    await db_session.flush()
    claim = _seed_claim(task, assignment, student.id)
    db_session.add(claim)
    await db_session.flush()
    submission = _seed_submission(claim)
    db_session.add(submission)
    await db_session.flush()
    claim.latest_submission_id = submission.id
    await db_session.flush()

    as_owner = await client.get(
        f"/api/v1/submissions/{submission.id}/download",
        headers=_bearer(student_tokens),
    )
    assert as_owner.status_code == 200, as_owner.text
    body = as_owner.json()
    assert set(body) == _DOWNLOAD_FIELDS
    assert body["url"]
    assert body["expires_at"]
    assert "object_key" not in body

    as_teacher = await client.get(
        f"/api/v1/submissions/{submission.id}/download",
        headers=_bearer(owner_tokens),
    )
    assert as_teacher.status_code == 200, as_teacher.text

    as_colleague = await client.get(
        f"/api/v1/submissions/{submission.id}/download",
        headers=_bearer(colleague_tokens),
    )
    assert as_colleague.status_code == 200

    for denied_tokens in (outsider_tokens, other_tokens):
        denied = await client.get(
            f"/api/v1/submissions/{submission.id}/download",
            headers=_bearer(denied_tokens),
        )
        assert denied.status_code == 403, denied.text
        assert _envelope(denied)["code"] == "PERMISSION_DENIED"

    missing = await client.get(
        f"/api/v1/submissions/{uuid4()}/download",
        headers=_bearer(student_tokens),
    )
    assert missing.status_code == 404
    assert _envelope(missing)["code"] == "NOT_FOUND"


# --- the teacher review queue (spec §28, §41) ----------------------------------------


@pytest.mark.integration
async def test_teacher_review_queue_context_fields_and_pagination(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    """VALIDATED-but-undecided submissions on own + collaborating tasks,
    oldest first, with claim/task context, the locked tier, the
    validation summary + preview, and the API download path; failed and
    already-decided submissions stay out; an unrelated teacher sees
    nothing."""
    owner, owner_tokens = await _seed_management_teacher(
        db_session, api_clock, username=_OWNER_EMAIL
    )
    other_owner, _ = await _seed_management_teacher(
        db_session, api_clock, username=_OTHER_OWNER_EMAIL
    )
    colleague, _ = await _seed_management_teacher(
        db_session, api_clock, username=_REVIEW_COLLEAGUE_EMAIL
    )
    outsider, outsider_tokens = await _seed_management_teacher(
        db_session, api_clock, username=_OUTSIDER_EMAIL
    )
    students = [
        await _seed_user(
            db_session, username=f"{_STUDENT_NUMBER}{index}", role=Role.STUDENT
        )
        for index in range(5)
    ]

    own_task = _seed_task(owner.id, title="自己任务")
    foreign_task = _seed_task(other_owner.id, title="协作任务")
    db_session.add_all([own_task, foreign_task])
    await db_session.flush()
    db_session.add(
        TaskCollaborator(
            task_id=foreign_task.id,
            teacher_id=owner.id,
            permissions=["REVIEW_SUBMISSIONS"],
        )
    )
    await db_session.flush()

    def _unit(task: Task, keyword: str) -> Assignment:
        return _seed_assignment(task.id, keyword)

    # Three reviewable submissions on the own task, staggered submitted_at.
    reviewable: list[Submission] = []
    for index, student in enumerate(students[:3]):
        assignment = _unit(own_task, f"关键词{index}")
        db_session.add(assignment)
        await db_session.flush()
        claim = _seed_claim(
            own_task,
            assignment,
            student.id,
            status=ClaimStatus.UNDER_REVIEW.value,
            reward_lock_status=RewardLockStatus.PROVISIONAL.value,
            reward_tier_locked=100,
            locked_reward_points=100,
            reward_locked_at=_T0,
        )
        db_session.add(claim)
        await db_session.flush()
        row = _seed_submission(
            claim,
            submitted_at=_T0 - timedelta(hours=3 - index),
            report=_report_json(
                row_count=index + 1,
                preview_rows=[[f"https://example.com/{index}", "标题"]],
            ),
        )
        db_session.add(row)
        await db_session.flush()
        claim.latest_submission_id = row.id
        reviewable.append(row)
    await db_session.flush()

    # One reviewable submission on the collaborated foreign task.
    foreign_assignment = _unit(foreign_task, "协作关键词")
    db_session.add(foreign_assignment)
    await db_session.flush()
    foreign_claim = _seed_claim(
        foreign_task,
        foreign_assignment,
        students[3].id,
        status=ClaimStatus.UNDER_REVIEW.value,
        reward_lock_status=RewardLockStatus.PROVISIONAL.value,
        reward_tier_locked=80,
        locked_reward_points=80,
        reward_locked_at=_T0 - timedelta(hours=1),
    )
    db_session.add(foreign_claim)
    await db_session.flush()
    foreign_row = _seed_submission(foreign_claim, submitted_at=_T0 - timedelta(hours=4))
    db_session.add(foreign_row)
    await db_session.flush()
    foreign_claim.latest_submission_id = foreign_row.id

    # Excluded shapes: machine-failed and already approved.
    failed_assignment = _unit(own_task, "失败关键词")
    db_session.add(failed_assignment)
    await db_session.flush()
    failed_claim = _seed_claim(
        own_task,
        failed_assignment,
        students[4].id,
        status=ClaimStatus.REVISION_REQUIRED.value,
    )
    db_session.add(failed_claim)
    await db_session.flush()
    failed_row = _seed_submission(
        failed_claim,
        submitted_at=_T0 - timedelta(minutes=30),
        validation_status="VALIDATION_FAILED",
        review_status="REVISION_REQUIRED",
        report=_report_json(row_count=0),
    )
    db_session.add(failed_row)
    await db_session.flush()
    failed_claim.latest_submission_id = failed_row.id

    approved_assignment = _unit(own_task, "通过关键词")
    db_session.add(approved_assignment)
    await db_session.flush()
    approved_claim = _seed_claim(
        own_task,
        approved_assignment,
        students[1].id,
        status=ClaimStatus.COMPLETED.value,
        reward_lock_status=RewardLockStatus.CONFIRMED.value,
        terminal_at=_T0,
    )
    db_session.add(approved_claim)
    await db_session.flush()
    approved_row = _seed_submission(
        approved_claim, submitted_at=_T0 - timedelta(hours=5), review_status="APPROVED"
    )
    db_session.add(approved_row)
    await db_session.flush()
    approved_claim.latest_submission_id = approved_row.id
    await db_session.flush()

    listed = await client.get(
        "/api/v1/teacher/submissions/review-queue", headers=_bearer(owner_tokens)
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert set(body) == _QUEUE_FIELDS
    assert body["total"] == 4
    assert body["limit"] == 20
    assert body["offset"] == 0
    # Oldest first: the foreign (-4h) then the own three (-3h, -2h, -1h).
    order = [item["submission_id"] for item in body["items"]]
    assert order == [
        str(foreign_row.id),
        str(reviewable[0].id),
        str(reviewable[1].id),
        str(reviewable[2].id),
    ]

    first = body["items"][0]
    assert set(first) == _QUEUE_ITEM_FIELDS
    assert first["claim_id"] == str(foreign_claim.id)
    assert first["task_id"] == str(foreign_task.id)
    assert first["task_title"] == "协作任务"
    assert first["platform"] == "xiaohongshu"
    assert first["keyword"] == "协作关键词"
    assert first["version"] == 1
    assert first["declared_type"] == "CSV"
    assert first["detected_type"] == "CSV"
    assert first["review_status"] == "PENDING_REVIEW"
    assert first["claim_status"] == "UNDER_REVIEW"
    assert first["reward_tier_locked"] == 80
    assert first["locked_reward_points"] == 80
    assert set(first["validation"]) == _REPORT_FIELDS
    assert first["validation"]["row_count"] == 2
    assert first["validation"]["preview_rows"] == []
    assert first["download_url"] == f"/api/v1/submissions/{foreign_row.id}/download"
    # Privacy scan: no object key rides along (spec §40).
    assert "object_key" not in first

    paged = await client.get(
        "/api/v1/teacher/submissions/review-queue",
        params={"limit": 2, "offset": 2},
        headers=_bearer(owner_tokens),
    )
    assert paged.status_code == 200, paged.text
    paged_body = paged.json()
    assert paged_body["total"] == 4
    assert [item["submission_id"] for item in paged_body["items"]] == [
        str(reviewable[1].id),
        str(reviewable[2].id),
    ]

    empty = await client.get(
        "/api/v1/teacher/submissions/review-queue", headers=_bearer(outsider_tokens)
    )
    assert empty.status_code == 200, empty.text
    assert empty.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}

    # The queue is a staff surface: students are refused at the guard.
    student_tokens = await _session_tokens(db_session, api_clock, students[0])
    denied = await client.get(
        "/api/v1/teacher/submissions/review-queue", headers=_bearer(student_tokens)
    )
    assert denied.status_code == 403
    assert _envelope(denied)["code"] == "PERMISSION_DENIED"

    # The collaborator who holds REVIEW_SUBMISSIONS on the foreign task
    # sees it too (owner visibility is own + collaborated).
    colleague_tokens = await _session_tokens(db_session, api_clock, colleague)
    colleague_view = await client.get(
        "/api/v1/teacher/submissions/review-queue", headers=_bearer(colleague_tokens)
    )
    assert colleague_view.status_code == 200, colleague_view.text
    assert colleague_view.json()["total"] == 0


# --- the review actions (spec §11.3, §14) ---------------------------------------------


async def _seed_reviewable(
    db: AsyncSession,
    clock: FrozenClock,
    *,
    owner_username: str = _OWNER_EMAIL,
) -> tuple[AssignmentClaim, Submission, dict]:
    """One machine-VALIDATED, lock-PROVISIONAL world ready for review."""
    owner, owner_tokens = await _seed_management_teacher(
        db, clock, username=owner_username
    )
    student = await _seed_user(db, username=_STUDENT_NUMBER, role=Role.STUDENT)
    task = _seed_task(owner.id)
    db.add(task)
    await db.flush()
    assignment = _seed_assignment(task.id, "考研经验")
    db.add(assignment)
    await db.flush()
    claim = _seed_claim(
        task,
        assignment,
        student.id,
        status=ClaimStatus.UNDER_REVIEW.value,
        reward_lock_status=RewardLockStatus.PROVISIONAL.value,
        reward_tier_locked=100,
        locked_reward_points=100,
        reward_locked_at=_T0,
    )
    db.add(claim)
    await db.flush()
    submission = _seed_submission(claim)
    db.add(submission)
    await db.flush()
    claim.latest_submission_id = submission.id
    await db.flush()
    return claim, submission, owner_tokens


@pytest.mark.integration
async def test_approve_completes_claim_and_grants_once(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
    fake_points: FakePointsRewardPort,
    event_collector: InMemoryEventCollector,
) -> None:
    claim, submission, owner_tokens = await _seed_reviewable(db_session, api_clock)

    approved = await client.post(
        f"/api/v1/teacher/submissions/{submission.id}/approve",
        headers=_bearer(owner_tokens),
    )
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert set(body) == _APPROVE_FIELDS
    assert body["claim_id"] == str(claim.id)
    assert body["claim_status"] == "COMPLETED"
    assert body["reward_lock_status"] == "CONFIRMED"
    assert body["points_granted"] == 100
    assert body["already_reviewed"] is False
    assert fake_points.grant_count == 1
    assert fake_points.calls[0].idempotency_key == f"assignment_reward:{claim.id}"

    # §14 idempotency: the replay answers already_reviewed, no second grant.
    replayed = await client.post(
        f"/api/v1/teacher/submissions/{submission.id}/approve",
        headers=_bearer(owner_tokens),
    )
    assert replayed.status_code == 200, replayed.text
    replay = replayed.json()
    assert replay["already_reviewed"] is True
    assert replay["points_granted"] is None
    assert fake_points.grant_count == 1

    await db_session.refresh(claim)
    assert claim.status == ClaimStatus.COMPLETED.value
    assert claim.terminal_at is not None
    assert [event.event_type for event in event_collector.events].count(
        "SUBMISSION_APPROVED"
    ) == 1


@pytest.mark.integration
async def test_revision_required_note_is_mandatory_and_sets_window(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    claim, submission, owner_tokens = await _seed_reviewable(db_session, api_clock)

    missing = await client.post(
        f"/api/v1/teacher/submissions/{submission.id}/revision-required",
        json={},
        headers=_bearer(owner_tokens),
    )
    assert missing.status_code == 422
    assert _envelope(missing)["code"] == "VALIDATION_ERROR"

    blank = await client.post(
        f"/api/v1/teacher/submissions/{submission.id}/revision-required",
        json={"note": "   "},
        headers=_bearer(owner_tokens),
    )
    assert blank.status_code == 422

    required = await client.post(
        f"/api/v1/teacher/submissions/{submission.id}/revision-required",
        json={"note": "缺少来源列，请补充。"},
        headers=_bearer(owner_tokens),
    )
    assert required.status_code == 200, required.text
    body = required.json()
    assert set(body) == _REVISION_FIELDS
    assert body["claim_id"] == str(claim.id)
    assert body["claim_status"] == "REVISION_REQUIRED"
    assert body["reward_lock_status"] == "PROVISIONAL"
    # §11.4: max(existing grace baseline, reviewed_at + 24h) — reviewing
    # well before grace keeps grace as the revision deadline (the
    # window never shrinks below it).
    assert datetime.fromisoformat(body["revision_deadline_at"]) == _T0 + timedelta(
        days=4
    )

    await db_session.refresh(claim)
    assert claim.status == ClaimStatus.REVISION_REQUIRED.value
    await db_session.refresh(submission)
    assert submission.review_status == "REVISION_REQUIRED"
    assert submission.review_note == "缺少来源列，请补充。"


@pytest.mark.integration
async def test_invalidate_reward_lock_requires_reason_and_cancels(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    claim, submission, owner_tokens = await _seed_reviewable(db_session, api_clock)

    missing = await client.post(
        f"/api/v1/teacher/submissions/{submission.id}/invalidate-reward-lock",
        json={},
        headers=_bearer(owner_tokens),
    )
    assert missing.status_code == 422
    assert _envelope(missing)["code"] == "VALIDATION_ERROR"

    # Whitespace is also refused by the transport shape rule (the
    # service's typed 400 stays the direct-caller answer — through the
    # API the blank never reaches it).
    blank = await client.post(
        f"/api/v1/teacher/submissions/{submission.id}/invalidate-reward-lock",
        json={"reason": "   "},
        headers=_bearer(owner_tokens),
    )
    assert blank.status_code == 422
    assert _envelope(blank)["code"] == "VALIDATION_ERROR"

    invalidated = await client.post(
        f"/api/v1/teacher/submissions/{submission.id}/invalidate-reward-lock",
        json={"reason": "空壳提交：仅含表头，无任何数据行。"},
        headers=_bearer(owner_tokens),
    )
    assert invalidated.status_code == 200, invalidated.text
    body = invalidated.json()
    assert set(body) == _REVISION_FIELDS
    assert body["claim_id"] == str(claim.id)
    assert body["claim_status"] == "REVISION_REQUIRED"
    assert body["reward_lock_status"] == "INVALIDATED"

    await db_session.refresh(claim)
    assert claim.reward_lock_status == RewardLockStatus.INVALIDATED.value
    assert claim.reward_tier_locked is None
    assert claim.locked_reward_points is None


@pytest.mark.integration
async def test_review_action_permission_matrix(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
    fake_points: FakePointsRewardPort,
) -> None:
    """Owner and a REVIEW_SUBMISSIONS collaborator may act; a VIEW_TASK
    collaborator, an unrelated teacher, and a Student may not (the
    student is refused at the staff guard)."""
    claim, submission, owner_tokens = await _seed_reviewable(db_session, api_clock)
    task = await db_session.get(Task, claim.task_id)
    assert task is not None
    colleague, colleague_tokens = await _seed_management_teacher(
        db_session, api_clock, username=_REVIEW_COLLEAGUE_EMAIL
    )
    viewer, viewer_tokens = await _seed_management_teacher(
        db_session, api_clock, username=_VIEW_COLLEAGUE_EMAIL
    )
    outsider, outsider_tokens = await _seed_management_teacher(
        db_session, api_clock, username=_OUTSIDER_EMAIL
    )
    student = await db_session.get(User, claim.user_id)
    assert student is not None
    student_tokens = await _session_tokens(db_session, api_clock, student)
    db_session.add_all(
        (
            TaskCollaborator(
                task_id=task.id,
                teacher_id=colleague.id,
                permissions=["REVIEW_SUBMISSIONS"],
            ),
            TaskCollaborator(
                task_id=task.id, teacher_id=viewer.id, permissions=["VIEW_TASK"]
            ),
        )
    )
    # Close the seeding savepoint segment: the service's denial path
    # rolls the session back, and a rollback would otherwise swallow
    # rows that were only flushed (the harness commits freely — the
    # outer transaction still rolls everything away at teardown).
    await db_session.commit()

    url = f"/api/v1/teacher/submissions/{submission.id}/revision-required"
    note = {"note": "请修改。"}
    # Captured before the requests: the service's denial path rolls the
    # session back, which expires every ORM instance in it.
    submission_id = submission.id
    for denied_tokens in (viewer_tokens, outsider_tokens, student_tokens):
        denied = await client.post(url, json=note, headers=_bearer(denied_tokens))
        assert denied.status_code == 403, denied.text
        assert _envelope(denied)["code"] == "PERMISSION_DENIED"

    # Nothing was written by the denials.
    await db_session.refresh(claim)
    assert claim.status == ClaimStatus.UNDER_REVIEW.value
    assert (
        await db_session.scalar(
            select(SubmissionReview).where(
                SubmissionReview.submission_id == submission_id
            )
        )
        is None
    )

    # The REVIEW_SUBMISSIONS collaborator may act (the owner's allow was
    # proven by the flow tests above; this pins the collaborator arm).
    allowed = await client.post(url, json=note, headers=_bearer(colleague_tokens))
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["claim_status"] == "REVISION_REQUIRED"


# --- rate limiting (spec §33.1) -------------------------------------------------------


@pytest.mark.integration
async def test_upload_intent_is_rate_limited_per_user(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
    fake_limiter: FakeRateLimiter,
) -> None:
    owner, _ = await _seed_management_teacher(
        db_session, api_clock, username=_OWNER_EMAIL
    )
    student = await _seed_user(db_session, username=_STUDENT_NUMBER, role=Role.STUDENT)
    student_tokens = await _session_tokens(db_session, api_clock, student)
    task = _seed_task(owner.id)
    db_session.add(task)
    await db_session.flush()
    assignment = _seed_assignment(task.id, "考研经验")
    db_session.add(assignment)
    await db_session.flush()
    claim = _seed_claim(task, assignment, student.id)
    db_session.add(claim)
    await db_session.flush()

    intended = await client.post(
        "/api/v1/submissions/upload-intent",
        json={
            "claim_id": str(claim.id),
            "filename": "数据.csv",
            "declared_type": "CSV",
            "size": len(_CSV_BYTES),
        },
        headers=_bearer(student_tokens),
    )
    assert intended.status_code == 201, intended.text
    checks = fake_limiter.checks_for("submissions:upload-intent")
    assert checks[-1].identifier == str(student.id)

    fake_limiter.fail_on("submissions:upload-intent")
    throttled = await client.post(
        "/api/v1/submissions/upload-intent",
        json={
            "claim_id": str(claim.id),
            "filename": "数据.csv",
            "declared_type": "CSV",
            "size": len(_CSV_BYTES),
        },
        headers=_bearer(student_tokens),
    )
    assert throttled.status_code == 429
    assert _envelope(throttled)["code"] == "RATE_LIMITED"


# --- unauthenticated surface ----------------------------------------------------------


@pytest.mark.integration
async def test_unauthenticated_submission_requests_are_rejected(
    client: httpx.AsyncClient,
) -> None:
    rejected = await client.post(
        "/api/v1/submissions/upload-intent",
        json={
            "claim_id": str(uuid4()),
            "filename": "x.csv",
            "declared_type": "CSV",
            "size": 1,
        },
    )
    assert rejected.status_code == 401
    assert _envelope(rejected)["code"] == "AUTHENTICATION_REQUIRED"

    teacher_surface = await client.get("/api/v1/teacher/submissions/review-queue")
    assert teacher_surface.status_code == 401


# --- the real job body chains the reward lock (worker wiring) -------------------------


_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)
_JOB_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)


def _new_factory() -> async_sessionmaker[AsyncSession]:
    """NullPool session factory: fresh connection per checkout, so the
    sync job's asyncio.run phase runs on its own event loop."""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.integration
def test_job_chains_reward_lock_after_validated_run() -> None:
    """``run_submission_validation`` — the real job body with injected
    dependencies — validates the upload AND immediately calls
    ``on_validation_passed``: the claim lands UNDER_REVIEW with a
    PROVISIONAL lock and the REWARD_LOCKED audit event reaches the
    publisher; a replayed run is fully idempotent."""
    from app.modules.submissions.reward_lock_service import REWARD_LOCKED
    from app.workers.jobs.validate_submission import run_submission_validation

    factory = _new_factory()
    run = uuid4().hex[:8]

    async def _seed(
        object_key_source: FakeObjectStorage,
    ) -> tuple[User, Task, AssignmentClaim, Submission]:
        async with factory() as session:
            teacher = User(
                username=f"t{run}",
                password_hash=_PASSWORD_HASH,
                nickname=f"老师{run[-4:]}",
                phone_e164=None,
                role=Role.TEACHER,
                status=UserStatus.ACTIVE,
            )
            student = User(
                username=f"2025{run}001",
                password_hash=_PASSWORD_HASH,
                nickname=f"同学{run[-4:]}",
                phone_e164=None,
                role=Role.STUDENT,
                status=UserStatus.ACTIVE,
            )
            session.add_all((teacher, student))
            await session.flush()
            task = _seed_task(
                teacher.id,
                submission_schema=_CSV_SCHEMA,
            )
            task.created_at = _JOB_NOW - timedelta(days=2)
            session.add(task)
            await session.flush()
            assignment = _seed_assignment(task.id, f"考研{run[-4:]}")
            session.add(assignment)
            await session.flush()
            claim = _seed_claim(
                task,
                assignment,
                student.id,
                claimed_at=_JOB_NOW - timedelta(days=1),
                deadline_at=_JOB_NOW + timedelta(days=3),
                grace_deadline_at=_JOB_NOW + timedelta(days=4),
            )
            session.add(claim)
            await session.flush()
            # The object key comes from the storage port (the presigned
            # flow's server-side shape), so the fake can pin its content
            # type and later replay the PUT content to the worker.
            url = object_key_source.create_upload_url(
                claim_id=claim.id,
                content_type="text/csv",
                expires_in=timedelta(minutes=10),
            )
            submission = Submission(
                claim_id=claim.id,
                version=1,
                object_key=url.object_key,
                original_filename="数据.csv",
                declared_type="CSV",
                file_size=len(_CSV_BYTES),
                submitted_at=_JOB_NOW,
                retention_until=_JOB_NOW + timedelta(days=180),
            )
            session.add(submission)
            await session.commit()
            return teacher, task, claim, submission

    storage = FakeObjectStorage()
    teacher, task, claim, submission = asyncio.run(_seed(storage))
    collector = InMemoryEventCollector()

    # The job's session seam: a FACTORY whose every call yields one
    # fresh session (one per asyncio.run invocation, NullPool so the
    # connection never crosses loops).
    def session_source() -> AsyncSession:
        return factory()

    try:
        # The seeded world is COMMITTED, so everything from here on —
        # including the fake-object PUT — sits inside the finally-guarded
        # region: a failure mid-test must still clean the rows.
        storage.put_object(object_key=submission.object_key, content=_CSV_BYTES)
        result = run_submission_validation(
            str(submission.id),
            request_id="req-job-chain-1",
            clock=FrozenClock(_JOB_NOW + timedelta(minutes=5)),
            storage=storage,
            session_source=session_source,
            events=collector,
        )
        assert result["validation_status"] == "VALIDATED"
        assert result["passed"] is True
        assert result["already_terminal"] is False

        async def _inspect() -> tuple[AssignmentClaim, int]:
            async with factory() as session:
                row = await session.get(AssignmentClaim, claim.id)
                assert row is not None
                runs = len(
                    (
                        await session.execute(
                            select(SubmissionValidation).where(
                                SubmissionValidation.submission_id == submission.id
                            )
                        )
                    ).all()
                )
                return row, runs

        locked, run_rows = asyncio.run(_inspect())
        assert locked.status == ClaimStatus.UNDER_REVIEW.value
        assert locked.reward_lock_status == RewardLockStatus.PROVISIONAL.value
        assert locked.reward_tier_locked == 100
        assert locked.locked_reward_points == 100
        assert locked.latest_submission_id == submission.id
        assert run_rows == 1
        assert [event.event_type for event in collector.of_type(REWARD_LOCKED)].count(
            REWARD_LOCKED
        ) == 1

        # The replay: terminal state returns the persisted report, the
        # reward-lock call is idempotent, and nothing duplicates.
        replay = run_submission_validation(
            str(submission.id),
            request_id="req-job-chain-1",
            clock=FrozenClock(_JOB_NOW + timedelta(minutes=6)),
            storage=storage,
            session_source=session_source,
            events=collector,
        )
        assert replay["already_terminal"] is True
        assert replay["validation_status"] == "VALIDATED"
        locked_again, run_rows_again = asyncio.run(_inspect())
        assert locked_again.status == ClaimStatus.UNDER_REVIEW.value
        assert run_rows_again == 1
        assert len(collector.of_type(REWARD_LOCKED)) == 1
    finally:

        async def _cleanup() -> None:
            async with factory() as session:
                await session.execute(
                    delete(SubmissionValidation).where(
                        SubmissionValidation.submission_id == submission.id
                    )
                )
                await session.execute(
                    delete(SubmissionReview).where(
                        SubmissionReview.submission_id == submission.id
                    )
                )
                await session.execute(
                    delete(RewardLockHistory).where(
                        RewardLockHistory.claim_id == claim.id
                    )
                )
                await session.execute(
                    delete(Submission).where(Submission.claim_id == claim.id)
                )
                await session.execute(
                    delete(AssignmentClaim).where(AssignmentClaim.task_id == task.id)
                )
                await session.execute(
                    delete(Assignment).where(Assignment.task_id == task.id)
                )
                await session.execute(delete(Task).where(Task.id == task.id))
                await session.execute(
                    delete(User).where(User.username.in_((f"t{run}", f"2025{run}001")))
                )
                await session.commit()

        asyncio.run(_cleanup())
