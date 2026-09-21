# backend/tests/integration/test_composition_smoke.py
"""Production-composition smoke (G18): the owner-required CI chain —
presign -> real HTTP PUT -> HEAD/finalize -> worker download -> validate
-> teacher approve — with NO core provider overridden.

Unlike ``test_submission_api.py`` (the faked-storage API suite) and the
worker suites that stub the service seam, this module drives
``create_app()`` with **zero ``dependency_overrides``** and calls the
validation job entry with **zero injected dependencies**:

- ``get_object_storage`` stays the production ``S3ObjectStorage`` built
  from Settings — the intent the API issues is a REAL presigned
  write-once PUT, and the job below downloads the REAL object back from
  MinIO;
- ``run_submission_validation`` runs with its production defaults: the
  real per-job engine/session source, the real storage factory, the
  real sandboxed validator subprocess, Settings-derived preview bounds;
- the validation dispatcher stays ``CeleryValidationDispatcher`` — the
  finalize really publishes the job onto the configured broker (proved
  by the queue depth), and the approve really publishes the ranking
  recompute; no worker is expected to consume them inside this suite;
- the endpoint rate limiter, business clock, session guard, and points
  ledger stay production bindings (Redis, SystemClock, real JWT +
  session rows, real ledger + wallet projection).

Harness base (the db_session test-harness allowance in the task brief):
committed NullPool sessions — the ``test_validation_worker`` convention,
NOT the rollback harness, because the chain crosses a real session
boundary (the job's per-job engine) that an uncommitted outer
transaction would make invisible. Every test removes its rows with
committed DELETEs in FK order in ``finally``; the S3 objects are deleted
through the real adapter and the published broker messages are purged,
so nothing leaks into later runs.

Composition gap recorded (brief step c): ``UploadIntentResponse``
carries exactly ``{intent_id, upload_url, expires_at}`` — the signed
headers the client MUST send with its PUT (``If-None-Match: *``, the
pinned Content-Type, the declared Content-Length) are NOT returned by
the API. A client can only reconstruct them from documentation
(interfaces.md's upload-PUT client contract plus the server-side
``DECLARED_TYPE_CONTENT_TYPES`` mapping). This test reconstructs them
the way a documented client must; changing the response contract is the
controller's call, not this suite's.

Skipped unless ``CQ_COMPOSITION_SMOKE=1`` (CI sets it; the default
local run must stay green without the full stack). Needs the MinIO,
PostgreSQL, and Redis of the integration stack.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import urllib.error
import urllib.request
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import get_async_engine, get_async_session_maker
from app.integrations.object_storage_s3 import S3ObjectStorage
from app.main import create_app
from app.modules.identity.dependencies import get_access_token_codec
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import TotpCredential, User, UserSession
from app.modules.identity.session_service import SessionService
from app.modules.notifications.models import Notification, NotificationDelivery
from app.modules.points.models import PointsLedger, PointWallet
from app.modules.rankings.honor_models import Honor, UserHonor
from app.modules.submissions.enums import FileType, ReviewStatus, ValidationStatus
from app.modules.submissions.models import (
    RewardLockHistory,
    Submission,
    SubmissionReview,
    SubmissionValidation,
    UploadIntent,
)
from app.modules.submissions.router import get_submissions_redis
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
from app.workers.jobs.validate_submission import run_submission_validation

if os.environ.get("CQ_COMPOSITION_SMOKE") != "1":
    pytest.skip(
        "production-composition smoke: set CQ_COMPOSITION_SMOKE=1 (with the "
        "integration PostgreSQL/Redis/MinIO stack up) to run",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration

_PASSWORD = "correct-horse-battery"

# A legal CSV against the seeded schema (url unique string + title
# string): two data rows, no findings -> the machine gate passes.
_CSV_BODY = (
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

# The frozen public wire shape of the intent response — asserted below as
# the composition-gap evidence: no signed-header field exists.
_INTENT_FIELDS = {"intent_id", "upload_url", "expires_at"}

# Celery's default queue name (no custom routing is configured): the
# finalize's real .delay publish lands here on the configured broker.
_BROKER_QUEUE_KEY = "celery"

# The client-side reconstruction of the signed PUT headers (interfaces.md
# upload-PUT client contract): the pinned MIME for the declared CSV type
# and the write-once condition. Content-Length is the body length.
_CSV_CONTENT_TYPE = "text/csv"


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Committed NullPool sessions (the test_validation_worker
    convention): fresh connection per checkout, so the API loop, the
    job's own asyncio.run loop, and these helpers never share pooled
    connections."""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _put(url: str, body: bytes) -> int:
    """Real HTTP PUT through the presigned URL exactly like the
    documented browser client: the pinned Content-Type, the body-derived
    Content-Length, and the mandatory signed If-None-Match:* header."""
    request = urllib.request.Request(
        url,
        data=body,
        method="PUT",
        headers={"Content-Type": _CSV_CONTENT_TYPE, "If-None-Match": "*"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


async def _seed_world(
    factory: async_sessionmaker[AsyncSession], run: str
) -> dict[str, Any]:
    """Direct-ORM seed (the existing fixture style): one ACTIVE teacher
    with a confirmed TOTP credential (the management guard), one ACTIVE
    student, one PUBLISHED RELATIVE task accepting CSV, one AVAILABLE
    assignment — plus live bearer sessions for both users. Committed:
    the job's own per-job session must see these rows."""
    now = datetime.now(UTC)
    async with factory() as db:
        teacher = User(
            username=f"t{run}",
            password_hash=hash_password(_PASSWORD),
            nickname=f"组合烟教师{run[:4]}",
            phone_e164=None,
            role=Role.TEACHER,
            status=UserStatus.ACTIVE,
        )
        student = User(
            username=f"2025{run}001",
            password_hash=hash_password(_PASSWORD),
            nickname=f"组合烟同学{run[:4]}",
            phone_e164=None,
            role=Role.STUDENT,
            status=UserStatus.ACTIVE,
        )
        db.add_all((teacher, student))
        await db.flush()
        db.add(
            TotpCredential(
                user_id=teacher.id,
                secret_encrypted=b"composition-smoke-stand-in",
                confirmed_at=now,
            )
        )
        task = Task(
            owner_teacher_id=teacher.id,
            title="小红书考研经验帖数据采集（组合烟）",
            description="采集指定关键词下的笔记正文与互动数据。",
            task_type=TaskType.DATA_CRAWL,
            rarity=TaskRarity.NORMAL,
            base_reward_points=100,
            status=TaskStatus.PUBLISHED,
            deadline_mode=DeadlineMode.RELATIVE,
            duration_minutes=4320,  # 3 days: submitted now is on-time (100%)
            submission_schema=_CSV_SCHEMA,
            submission_schema_version=1,
            allowed_file_types=["CSV"],
            max_file_size_bytes=10 * 1024 * 1024,
            notification_channels=["SMS"],
            published_at=now - timedelta(days=1),
        )
        db.add(task)
        await db.flush()
        assignment = Assignment(
            task_id=task.id,
            platform="xiaohongshu",
            keyword=f"考研{run[:4]}",
            availability_status=AssignmentAvailability.AVAILABLE,
        )
        db.add(assignment)
        await db.flush()

        sessions = SessionService(
            clock=SystemClock(), access_codec=get_access_token_codec()
        )
        _, teacher_tokens = await sessions.issue_session(
            db, user=teacher, now=SystemClock().now()
        )
        _, student_tokens = await sessions.issue_session(
            db, user=student, now=SystemClock().now()
        )
        await db.commit()
        return {
            "teacher_id": teacher.id,
            "student_id": student.id,
            "task_id": task.id,
            "assignment_id": assignment.id,
            # Snapshot of the GLOBAL honor-definition rows: the approve's
            # post-commit honors trigger lazily creates definitions
            # (once per key, process-wide), and cleanup must remove only
            # the ones THIS run created — leaving them behind collides
            # with the rankings suite's own definition fixtures.
            "honor_ids_before": set(
                (await db.execute(select(Honor.id).order_by(Honor.created_at)))
                .scalars()
                .all()
            ),
            "teacher_headers": {
                "Authorization": f"Bearer {teacher_tokens.access_token}"
            },
            "student_headers": {
                "Authorization": f"Bearer {student_tokens.access_token}"
            },
        }


async def _cleanup(
    factory: async_sessionmaker[AsyncSession], world: dict[str, Any]
) -> None:
    """Remove this test's committed rows in FK order (the
    test_validation_worker convention) so the shared test database and
    MinIO bucket carry nothing from a composition run."""
    task_ids = [world["task_id"]]
    user_ids = [world["teacher_id"], world["student_id"]]
    async with factory() as db:
        claim_ids = select(AssignmentClaim.id).where(
            AssignmentClaim.task_id.in_(task_ids)
        )
        submission_ids = select(Submission.id).where(Submission.claim_id.in_(claim_ids))
        # upload_intents FIRST: the finalized intent's
        # finalized_submission_id FK points at submissions.
        await db.execute(
            delete(UploadIntent).where(UploadIntent.claim_id.in_(claim_ids))
        )
        await db.execute(
            delete(SubmissionValidation).where(
                SubmissionValidation.submission_id.in_(submission_ids)
            )
        )
        await db.execute(
            delete(SubmissionReview).where(
                SubmissionReview.submission_id.in_(submission_ids)
            )
        )
        await db.execute(
            delete(RewardLockHistory).where(RewardLockHistory.claim_id.in_(claim_ids))
        )
        await db.execute(delete(Submission).where(Submission.claim_id.in_(claim_ids)))
        await db.execute(
            delete(AssignmentClaim).where(AssignmentClaim.task_id.in_(task_ids))
        )
        await db.execute(delete(Assignment).where(Assignment.task_id.in_(task_ids)))
        await db.execute(delete(Task).where(Task.id.in_(task_ids)))
        await db.execute(
            delete(NotificationDelivery).where(
                NotificationDelivery.user_id.in_(user_ids)
            )
        )
        await db.execute(delete(Notification).where(Notification.user_id.in_(user_ids)))
        await db.execute(delete(PointsLedger).where(PointsLedger.user_id.in_(user_ids)))
        await db.execute(delete(PointWallet).where(PointWallet.user_id.in_(user_ids)))
        # The grants first, then the definitions this run lazily created
        # (the snapshot delta — never another test's seeded rows).
        await db.execute(delete(UserHonor).where(UserHonor.user_id.in_(user_ids)))
        honor_ids_now = set((await db.execute(select(Honor.id))).scalars().all())
        created_honor_ids = honor_ids_now - set(world["honor_ids_before"])
        if created_honor_ids:
            await db.execute(delete(Honor).where(Honor.id.in_(created_honor_ids)))
        await db.execute(delete(UserSession).where(UserSession.user_id.in_(user_ids)))
        await db.execute(
            delete(TotpCredential).where(TotpCredential.user_id.in_(user_ids))
        )
        await db.execute(delete(User).where(User.id.in_(user_ids)))
        await db.commit()


async def _purge_seeded_objects(
    factory: async_sessionmaker[AsyncSession], world: dict[str, Any]
) -> None:
    """Delete the objects this run PUT, through the real adapter (§27:
    an absent object raises, so each delete is guarded — a rejected or
    never-used intent must not fail the cleanup)."""
    storage = S3ObjectStorage(get_settings())
    async with factory() as db:
        keys = (
            (
                await db.execute(
                    select(UploadIntent.object_key).where(
                        UploadIntent.claim_id.in_(
                            select(AssignmentClaim.id).where(
                                AssignmentClaim.task_id == world["task_id"]
                            )
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
    for key in keys:
        with contextlib.suppress(FileNotFoundError):
            await asyncio.to_thread(storage.delete_object, object_key=key)


async def test_presign_put_finalize_validate_approve_composition(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The whole owner-required chain against real wiring: real claim,
    real presign, real HTTP PUT (plus the write-once 412 replay), real
    finalize HEAD, the real job entry downloading from MinIO into the
    sandboxed validator, and the real teacher approve — ending on the
    projected wallet points."""
    settings = get_settings()
    run = uuid.uuid4().hex[:12]
    app = create_app()
    broker = aioredis.from_url(settings.redis_url, decode_responses=True)
    world = await _seed_world(factory, run)
    submission_id: str | None = None
    claim_id: str | None = None
    try:
        queue_depth_before = await broker.llen(_BROKER_QUEUE_KEY)

        # The app's lifespan is production startup: it binds the shared
        # Celery app so the dispatcher's .delay resolves to the
        # settings-configured broker (never Celery's amqp fallback).
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://composition"
            ) as client:
                # (b) the student claims a random assignment through the
                # real route (real guard, real limiter, real commit).
                claimed = await client.post(
                    f"/api/v1/tasks/{world['task_id']}/claim",
                    headers=world["student_headers"],
                )
                assert claimed.status_code == 201, claimed.text
                claim = claimed.json()
                claim_id = claim["claim_id"]
                assert claim["status"] == ClaimStatus.CLAIMED.value
                assert claim["base_reward_points_snapshot"] == 100

                # (c) upload intent through the real route: the storage
                # provider is the REAL S3ObjectStorage, so the response's
                # URL is a real presigned write-once PUT. The frozen
                # response shape is also the composition-gap evidence:
                # the signed PUT headers are NOT conveyed to the client.
                intent_response = await client.post(
                    "/api/v1/submissions/upload-intent",
                    headers=world["student_headers"],
                    json={
                        "claim_id": claim_id,
                        "filename": "采集结果.csv",
                        "declared_type": FileType.CSV.value,
                        "size": len(_CSV_BODY),
                    },
                )
                assert intent_response.status_code == 201, intent_response.text
                intent = intent_response.json()
                assert set(intent) == _INTENT_FIELDS
                assert intent["upload_url"].startswith(
                    f"{settings.s3_endpoint_url}/{settings.s3_bucket}/"
                )

                # (d) the real HTTP PUT with the client-reconstructed
                # signed headers, then the write-once re-proof at the
                # composition layer: the SAME business-issued URL admits
                # exactly one successful PUT.
                put_status = await asyncio.to_thread(
                    _put, intent["upload_url"], _CSV_BODY
                )
                assert put_status == 200
                replay_status = await asyncio.to_thread(
                    _put, intent["upload_url"], _CSV_BODY
                )
                assert replay_status == 412

                # (e) finalize through the real route: the REAL HEAD
                # verifies the stored object (size + pinned type), the
                # Submission is created, and the REAL dispatcher
                # publishes the validation job to the REAL broker.
                completed = await client.post(
                    "/api/v1/submissions/upload-complete",
                    headers=world["student_headers"],
                    json={"intent_id": intent["intent_id"]},
                )
                assert completed.status_code == 200, completed.text
                submission = completed.json()
                submission_id = submission["id"]
                assert submission["version"] == 1
                assert submission["file_size"] == len(_CSV_BODY)
                assert (
                    submission["validation_status"] == ValidationStatus.UPLOADED.value
                )
                assert submission["review_status"] == ReviewStatus.PENDING_REVIEW.value
                queue_depth_after_finalize = await broker.llen(_BROKER_QUEUE_KEY)
                assert queue_depth_after_finalize >= queue_depth_before + 1

                # (f) the job entry with EVERY default: real per-job
                # engine/session, real storage factory (the download
                # pulls the object this test PUT back from MinIO), real
                # sandboxed validator subprocess, real settings.
                job_result = await asyncio.to_thread(
                    run_submission_validation,
                    submission_id,
                    request_id=f"composition-smoke-{run}",
                )
                assert job_result["validation_status"] == "VALIDATED"
                assert job_result["passed"] is True
                assert job_result["detected_type"] == FileType.CSV.value
                assert job_result["row_count"] == 2
                assert job_result["already_terminal"] is False

                # The owner reads the persisted §12.4 report through the
                # real route (no object key, no parser internal leaks).
                validation_view = await client.get(
                    f"/api/v1/submissions/{submission_id}/validation",
                    headers=world["student_headers"],
                )
                assert validation_view.status_code == 200, validation_view.text
                report = validation_view.json()
                assert report["validation_status"] == ValidationStatus.VALIDATED.value
                assert report["detected_type"] == FileType.CSV.value
                assert report["report"]["row_count"] == 2
                assert report["report"]["errors"] == []
                assert "submissions/" not in validation_view.text

                # (g) the teacher approves through the real route: the
                # real review transaction confirms the lock and grants
                # through the REAL points ledger (wallet projection +
                # a second real broker publish for the ranking job).
                approved = await client.post(
                    f"/api/v1/teacher/submissions/{submission_id}/approve",
                    headers=world["teacher_headers"],
                )
                assert approved.status_code == 200, approved.text
                decision = approved.json()
                assert decision["claim_status"] == ClaimStatus.COMPLETED.value
                assert (
                    decision["reward_lock_status"] == RewardLockStatus.CONFIRMED.value
                )
                assert decision["points_granted"] == 100
                assert decision["already_reviewed"] is False

                # Idempotent replay through the API: nothing written, no
                # second grant (the row-lock serialization, composed).
                replayed = await client.post(
                    f"/api/v1/teacher/submissions/{submission_id}/approve",
                    headers=world["teacher_headers"],
                )
                assert replayed.status_code == 200
                assert replayed.json()["already_reviewed"] is True

        # (h) end-state assertions straight against the database: the
        # status chain, the lock tier, the append-only audit rows, and
        # the wallet/points projection.
        async with factory() as db:
            row = await db.get(Submission, uuid.UUID(submission_id))
            assert row is not None
            assert row.validation_status == ValidationStatus.VALIDATED.value
            assert row.review_status == ReviewStatus.APPROVED.value
            assert row.reviewer_id == world["teacher_id"]

            claim_row = await db.get(AssignmentClaim, uuid.UUID(claim_id))
            assert claim_row is not None
            assert claim_row.status == ClaimStatus.COMPLETED.value
            assert claim_row.terminal_at is not None
            assert claim_row.reward_lock_status == RewardLockStatus.CONFIRMED.value
            assert claim_row.reward_tier_locked == 100
            assert claim_row.locked_reward_points == 100
            assert claim_row.latest_submission_id == row.id

            lock_history = (
                (
                    await db.execute(
                        select(RewardLockHistory)
                        .where(RewardLockHistory.claim_id == claim_row.id)
                        .order_by(RewardLockHistory.created_at)
                    )
                )
                .scalars()
                .all()
            )
            assert [
                (transition.lock_status_from, transition.lock_status_to)
                for transition in lock_history
            ] == [
                (RewardLockStatus.NONE.value, RewardLockStatus.PROVISIONAL.value),
                (RewardLockStatus.PROVISIONAL.value, RewardLockStatus.CONFIRMED.value),
            ]
            assert all(t.reward_tier_locked == 100 for t in lock_history)

            review_rows = (
                (
                    await db.execute(
                        select(SubmissionReview).where(
                            SubmissionReview.submission_id == row.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert [review.action for review in review_rows] == ["APPROVE"]
            assert review_rows[0].reviewer_id == world["teacher_id"]

            ledger_entries = (
                (
                    await db.execute(
                        select(PointsLedger).where(
                            PointsLedger.user_id == world["student_id"]
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert [entry.amount for entry in ledger_entries] == [100]
            assert ledger_entries[0].source_id == claim_row.id

            wallet = await db.get(PointWallet, world["student_id"])
            assert wallet is not None
            assert wallet.available_points == 100
            assert wallet.earned_points == 100

            notifications = (
                (
                    await db.execute(
                        select(Notification.event_type).where(
                            Notification.user_id == world["student_id"]
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert "SUBMISSION_APPROVED" in notifications
    finally:
        await _purge_seeded_objects(factory, world)
        await _cleanup(factory, world)
        with contextlib.suppress(Exception):
            await broker.delete(_BROKER_QUEUE_KEY)
        await broker.aclose()
        # Restore the process-wide production singletons this test
        # populated: the pooled engine and the endpoint-limiter Redis
        # client bind to THIS test's event loop, and a later sync test
        # (readiness, on a fresh loop) would otherwise inherit dead-loop
        # connections through their lru_caches.
        with contextlib.suppress(Exception):
            await get_submissions_redis().aclose()
        get_submissions_redis.cache_clear()
        with contextlib.suppress(Exception):
            await get_async_engine().dispose()
        get_async_engine.cache_clear()
        get_async_session_maker.cache_clear()
