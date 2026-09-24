# backend/tests/e2e/test_worker_retries.py
"""Worker retry and external-failure gate (plan 10 task 7).

Every case drives the REAL worker job entry — the Celery task body or
the ``run_*`` orchestration core the beat schedule fires — against the
real PostgreSQL/MinIO stack, with the provider OUTAGE simulated
exactly the way the plan's interface prescribes: the tests/fakes
adapters' ``fail_with`` programming (the §13 taxonomy) stands in for
SMS/storage providers, because no e2e environment may depend on a real
outage. World-building (claims, uploads, notification rows) goes
through the public APIs or the e2e factories; the five release-gate
invariants:

1. **Duplicate notification job** — the same ``send_notification_delivery``
   job fired twice produces ONE provider message and one delivery row
   (event_key + channel dedupe, spec §25.3), and PG ends SENT with
   attempts == 1.
2. **Provider timeout sequence** — three programmed UnknownOutcomeError
   sends walk the §25.4 ladder (RETRYABLE ~1m, ~5m) and land terminal
   FAILED with bounded attempts == 3, while the business Task/Claim
   rows the notification describes never move.
3. **Duplicate validation job** — the same ``run_submission_validation``
   run twice writes ONE report/run row and replays the terminal state
   (``already_terminal``), never a second validation.
4. **Duplicate cleanup job** — a rerun over an already-deleted object
   reconciles per §27 (missing object -> marked deleted with the
   warning) and a rerun after a completed deletion leaves the row's
   metadata (filename, sizes, reports, intent) untouched.
5. **Crash simulation** — a sender that records the external message
   and THEN dies (before the finalize transaction) leaves the row
   SENDING; the re-run re-claims the stale lease and re-sends under
   the SAME deterministic provider idempotency key, and the provider
   model collapses the retry onto ONE externally recorded message.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.integrations.errors import UnknownOutcomeError
from app.integrations.object_storage_s3 import S3ObjectStorage
from app.main import create_app
from app.modules.notifications.delivery_service import provider_idempotency_key
from app.modules.notifications.enums import DeliveryStatus, NotificationChannel
from app.modules.notifications.models import NotificationDelivery
from app.modules.submissions.enums import FileType, ValidationStatus
from app.modules.submissions.models import Submission, SubmissionValidation
from app.modules.tasks.enums import ClaimStatus
from app.modules.tasks.models import AssignmentClaim
from app.workers.jobs.cleanup_files import cleanup_files_scan
from app.workers.jobs.send_notification import send_notification_delivery
from app.workers.jobs.validate_submission import run_submission_validation
from tests.e2e.factories import (
    TaskFixture,
    clean_world,
    seed_claim,
    seed_student,
    seed_task_with_assignments,
    seed_teacher_confirmed_totp,
    snapshot_honor_ids,
)
from tests.e2e.test_happy_path import _mint_access_token, _purge_objects, _put
from tests.fakes.integrations import FakeSmsSender

pytestmark = pytest.mark.e2e

_BROKER_QUEUE_KEY = "celery"

_GOOD_CSV = (
    b"url,title\n"
    b"https://example.com/note/1,\xe7\xac\xac\xe4\xb8\x80\xe6\x9d\xa1\n"
    b"https://example.com/note/2,\xe7\xac\xac\xe4\xba\x8c\xe6\x9d\xa1\n"
)


class _DedupingSmsSender(FakeSmsSender):
    """The provider-side view of idempotency keys: a real provider
    collapses retries that carry the same key onto ONE delivered
    message. ``calls`` records every attempted send (with its key) so
    tests can prove the retry REALLY re-sent under the SAME key;
    ``messages`` holds only the collapsed unique-key deliveries — the
    provider's external record."""

    def __init__(self) -> None:
        super().__init__()
        self.sent_keys: list[str] = []
        self.calls: list[str | None] = []

    def send(
        self,
        *,
        to: str,
        template: str,
        variables: Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> None:
        self._raise_if_programmed()
        self.calls.append(idempotency_key)
        if idempotency_key is None or idempotency_key not in self.sent_keys:
            if idempotency_key is not None:
                self.sent_keys.append(idempotency_key)
            self.messages.append(
                _RecordedMessage(to=to, template=template, key=idempotency_key)
            )


class _RecordedMessage:
    """A minimal recorded external message (to/template/key)."""

    def __init__(self, *, to: str, template: str, key: str | None) -> None:
        self.to = to
        self.template = template
        self.idempotency_key = key


class _CrashingAfterRecordSmsSender(_DedupingSmsSender):
    """The crash shape: the external call SUCCEEDS (the provider keeps
    the message under its idempotency key) and the worker dies right
    after — before the finalize transaction could ever run."""

    def send(
        self,
        *,
        to: str,
        template: str,
        variables: Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> None:
        super().send(
            to=to,
            template=template,
            variables=variables,
            idempotency_key=idempotency_key,
        )
        raise RuntimeError("simulated worker crash after the provider call")


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


async def _run_send_job(delivery_id: UUID, request_id: str) -> dict[str, Any]:
    """The REAL Celery task body (own engine, own event loop — hence the
    worker thread, the browser-world convention)."""
    return await asyncio.to_thread(
        send_notification_delivery.run, str(delivery_id), request_id
    )


async def _due_sms_delivery(
    db_factory: async_sessionmaker[AsyncSession], *, claim_id: UUID
) -> NotificationDelivery:
    """The claim flow's real deadline-reminder SMS delivery, made due
    now (the T8 due-scan shape: it re-enqueues exactly the rows whose
    scheduled_at has arrived)."""
    async with db_factory() as db:
        delivery = await db.scalar(
            select(NotificationDelivery)
            .where(
                NotificationDelivery.event_key.like(f"claim:{claim_id}%"),
                NotificationDelivery.channel == NotificationChannel.SMS.value,
            )
            .order_by(NotificationDelivery.scheduled_at)
        )
        assert delivery is not None, "the claim flow must plan SMS reminders"
        delivery.scheduled_at = datetime.now(UTC) - timedelta(minutes=1)
        await db.commit()
        return delivery


async def _set_delivery(
    db_factory: async_sessionmaker[AsyncSession],
    delivery_id: UUID,
    *,
    scheduled_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> None:
    """Accelerate the row's clock-sensitive columns (the due-scan /
    stale-lease shapes) without touching any status bookkeeping."""
    values: dict[str, Any] = {}
    if scheduled_at is not None:
        values["scheduled_at"] = scheduled_at
    if updated_at is not None:
        values["updated_at"] = updated_at
    if not values:
        return
    async with db_factory() as db:
        await db.execute(
            update(NotificationDelivery)
            .where(NotificationDelivery.id == delivery_id)
            .values(**values)
        )
        await db.commit()


async def _delivery_row(
    db_factory: async_sessionmaker[AsyncSession], delivery_id: UUID
) -> NotificationDelivery:
    async with db_factory() as db:
        row = await db.get(NotificationDelivery, delivery_id)
        assert row is not None
        return row


async def _seed_world(
    db_factory: async_sessionmaker[AsyncSession], run: str
) -> tuple[UUID, UUID, dict[str, str], list[UUID]]:
    """Teacher + student + two published CSV tasks (one claim slot each
    for the cases that need distinct submissions; a student may hold
    only one non-terminal claim per task) + student headers; returns
    (teacher_id, student_id, student_headers, task_ids)."""
    teacher = await seed_teacher_confirmed_totp(db_factory, run=run)
    student = await seed_student(db_factory, run=run)
    task_ids = []
    for suffix, count in (("a", 2), ("b", 1)):
        task = await seed_task_with_assignments(
            db_factory,
            teacher_id=teacher.user_id,
            run=f"{run}{suffix}",
            assignment_count=count,
        )
        task_ids.append(task.task_id)
    headers = {
        "Authorization": (
            f"Bearer {await _mint_access_token(db_factory, student.user_id)}"
        )
    }
    return teacher.user_id, student.user_id, headers, task_ids


# --- case 1: duplicate notification job ---------------------------------------------


async def test_duplicate_notification_job_delivers_exactly_once(
    db_factory: async_sessionmaker[AsyncSession],
    _stack: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same send job fired twice: one provider message, one delivery
    row (event_key + channel), terminal SENT with attempts == 1."""
    run = uuid.uuid4().hex[:10]
    honors_before = await snapshot_honor_ids(db_factory)
    _teacher, student_id, headers, task_ids = await _seed_world(db_factory, run)
    sms = FakeSmsSender()
    monkeypatch.setattr(
        "app.workers.jobs.send_notification.build_delivery_service",
        lambda **_: _patched_service(db_factory, sms),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_stack["app"]),
            base_url="http://e2e-retries",
        ) as client:
            claimed = await client.post(
                f"/api/v1/tasks/{task_ids[0]}/claim", headers=headers
            )
            assert claimed.status_code == 201, claimed.text
            claim_id = uuid.UUID(claimed.json()["claim_id"])

        delivery = await _due_sms_delivery(db_factory, claim_id=claim_id)

        first = await _run_send_job(delivery.id, f"e2e-retry-dup1-{run}")
        second = await _run_send_job(delivery.id, f"e2e-retry-dup2-{run}")

        assert first["outcome"] == "SENT", first
        assert second["outcome"] == "ALREADY_SENT", second

        # ONE external message, under the deterministic idempotency key.
        assert len(sms.messages) == 1, [m.idempotency_key for m in sms.messages]
        expected_key = provider_idempotency_key(
            delivery.event_key, NotificationChannel.SMS, student_id
        )
        assert sms.messages[0].idempotency_key == expected_key

        row = await _delivery_row(db_factory, delivery.id)
        assert row.status == DeliveryStatus.SENT.value
        assert row.attempts == 1

        async with db_factory() as db:
            same_triple = await db.execute(
                select(NotificationDelivery.id).where(
                    NotificationDelivery.event_key == delivery.event_key,
                    NotificationDelivery.user_id == student_id,
                    NotificationDelivery.channel == NotificationChannel.SMS.value,
                )
            )
            assert len(same_triple.all()) == 1  # UNIQUE(event_key, user_id, channel)
    finally:
        await clean_world(
            db_factory,
            user_ids=[_teacher, student_id],
            task_ids=task_ids,
            honor_ids_before=honors_before,
        )


def _patched_service(
    db_factory: async_sessionmaker[AsyncSession], sms: FakeSmsSender
) -> Any:
    """The real job's service, rebuilt around the fake provider.

    Mirrors ``build_delivery_service``'s production composition (the
    real deadline-suppression claim-status resolver, the SystemClock,
    the 15-minute stale-claim threshold the settings wire) with two
    test substitutions: the SMS/EMAIL senders become the fakes, and
    the session maker becomes this test's NullPool factory (the same
    per-job engine discipline the worker's ``run_with_session_maker``
    follows — the job's own maker is unused when the builder is
    patched, exactly the workers-suite contract).
    """
    from datetime import timedelta

    from app.core.clock import SystemClock
    from app.modules.notifications.delivery_service import DeliveryService

    async def _claim_status(claim_id: UUID) -> str | None:
        async with db_factory() as session:
            return await session.scalar(
                select(AssignmentClaim.status).where(AssignmentClaim.id == claim_id)
            )

    return DeliveryService(
        session_maker=db_factory,
        sms_sender=sms,
        email_sender=sms,  # unused in these SMS-only flows
        clock=SystemClock(),
        stale_claim_threshold=timedelta(minutes=15),
        claim_status_resolver=_claim_status,
    )


# --- case 2: provider timeout ladder -------------------------------------------------


async def test_provider_timeout_sequence_fails_bounded(
    db_factory: async_sessionmaker[AsyncSession],
    _stack: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three UnknownOutcomeError sends: RETRYABLE (~1m rung), RETRYABLE
    (~5m rung), then terminal FAILED at attempts == 3 — and the
    business claim/task rows never move."""
    run = uuid.uuid4().hex[:10]
    honors_before = await snapshot_honor_ids(db_factory)
    _teacher, student_id, headers, task_ids = await _seed_world(db_factory, run)
    sms = FakeSmsSender()
    sms.fail_with(UnknownOutcomeError("simulated provider timeout"), times=3)
    monkeypatch.setattr(
        "app.workers.jobs.send_notification.build_delivery_service",
        lambda **_: _patched_service(db_factory, sms),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_stack["app"]),
            base_url="http://e2e-retries",
        ) as client:
            claimed = await client.post(
                f"/api/v1/tasks/{task_ids[0]}/claim", headers=headers
            )
            assert claimed.status_code == 201, claimed.text
            claim_id = uuid.UUID(claimed.json()["claim_id"])

        delivery = await _due_sms_delivery(db_factory, claim_id=claim_id)

        first = await _run_send_job(delivery.id, f"e2e-retry-t1-{run}")
        assert first["outcome"] == "RETRY_SCHEDULED", first
        assert first["status"] == DeliveryStatus.RETRYABLE.value
        assert first["attempts"] == 1

        # The ladder's due instant arrives (the due-scan re-enqueue
        # shape): make the row due again and re-fire the job.
        await _set_delivery(
            db_factory,
            delivery.id,
            scheduled_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        second = await _run_send_job(delivery.id, f"e2e-retry-t2-{run}")
        assert second["outcome"] == "RETRY_SCHEDULED", second
        assert second["attempts"] == 2

        await _set_delivery(
            db_factory,
            delivery.id,
            scheduled_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        third = await _run_send_job(delivery.id, f"e2e-retry-t3-{run}")
        assert third["outcome"] == "FAILED", third
        assert third["status"] == DeliveryStatus.FAILED.value

        row = await _delivery_row(db_factory, delivery.id)
        assert row.status == DeliveryStatus.FAILED.value
        assert row.attempts == 3  # bounded: DEFAULT_MAX_ATTEMPTS
        assert row.last_error is not None and row.last_error.startswith(
            "unknown_outcome:"
        )
        # No message ever landed externally (every attempt timed out).
        assert sms.messages == []

        # Business state untouched through the whole outage.
        claim = await _claim_row(db_factory, claim_id)
        assert claim.status == ClaimStatus.CLAIMED.value
    finally:
        await clean_world(
            db_factory,
            user_ids=[_teacher, student_id],
            task_ids=task_ids,
            honor_ids_before=honors_before,
        )


async def _claim_row(
    db_factory: async_sessionmaker[AsyncSession], claim_id: UUID
) -> AssignmentClaim:
    async with db_factory() as db:
        claim = await db.get(AssignmentClaim, claim_id)
        assert claim is not None
        return claim


# --- case 3: duplicate validation job ------------------------------------------------


async def test_duplicate_validation_job_reports_once(
    db_factory: async_sessionmaker[AsyncSession], _stack: dict[str, Any]
) -> None:
    """The same validation job run twice: one run row, one persisted
    report, and the replay answers the terminal state."""
    run = uuid.uuid4().hex[:10]
    honors_before = await snapshot_honor_ids(db_factory)
    teacher, student_id, headers, task_ids = await _seed_world(db_factory, run)
    claim = await seed_claim(
        db_factory,
        task=await _task_fixture(db_factory, task_ids[0]),
        student_id=student_id,
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_stack["app"]),
            base_url="http://e2e-retries",
        ) as client:
            intent_response = await client.post(
                "/api/v1/submissions/upload-intent",
                headers=headers,
                json={
                    "claim_id": str(claim.claim_id),
                    "filename": "重试校验.csv",
                    "declared_type": FileType.CSV.value,
                    "size": len(_GOOD_CSV),
                },
            )
            assert intent_response.status_code == 201, intent_response.text
            intent = intent_response.json()
            put_status = await asyncio.to_thread(
                _put, intent["upload_url"], _GOOD_CSV, intent["headers"], len(_GOOD_CSV)
            )
            assert put_status == 200, put_status
            completed = await client.post(
                "/api/v1/submissions/upload-complete",
                headers=headers,
                json={"intent_id": intent["intent_id"]},
            )
            assert completed.status_code == 200, completed.text
            submission_id = completed.json()["id"]

        first = await asyncio.to_thread(
            run_submission_validation,
            submission_id,
            request_id=f"e2e-retry-v1-{run}",
        )
        second = await asyncio.to_thread(
            run_submission_validation,
            submission_id,
            request_id=f"e2e-retry-v2-{run}",
        )

        assert first["validation_status"] == "VALIDATED", first
        assert first["already_terminal"] is False, first
        assert second["validation_status"] == "VALIDATED", second
        assert second["already_terminal"] is True, second

        async with db_factory() as db:
            runs = (
                (
                    await db.execute(
                        select(SubmissionValidation).where(
                            SubmissionValidation.submission_id
                            == uuid.UUID(submission_id)
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(runs) == 1, [r.parser_version for r in runs]
            submission = await db.get(Submission, uuid.UUID(submission_id))
            assert submission is not None
            assert submission.validation_status == ValidationStatus.VALIDATED.value
            assert submission.validation_report is not None
    finally:
        await _purge_objects(db_factory, task_ids)
        await clean_world(
            db_factory,
            user_ids=[teacher, student_id],
            task_ids=task_ids,
            honor_ids_before=honors_before,
        )


async def _task_fixture(
    db_factory: async_sessionmaker[AsyncSession], task_id: UUID
) -> TaskFixture:
    """A ``TaskFixture`` view over one seeded task (``seed_claim``
    claims ``assignment_ids[0]``, so each call needs the task's own
    first assignment)."""
    from app.modules.tasks.models import Assignment

    async with db_factory() as db:
        assignment_ids = (
            (
                await db.execute(
                    select(Assignment.id)
                    .where(Assignment.task_id == task_id)
                    .order_by(Assignment.keyword)
                )
            )
            .scalars()
            .all()
        )
    return TaskFixture(
        task_id=task_id,
        assignment_ids=[list(assignment_ids)[0]],
        owner_teacher_id=uuid.UUID(int=0),
    )


# --- case 4: duplicate cleanup job ---------------------------------------------------


async def test_duplicate_cleanup_job_reconciles_without_metadata_loss(
    db_factory: async_sessionmaker[AsyncSession], _stack: dict[str, Any]
) -> None:
    """A completed deletion followed by a duplicate scan is a no-op;
    an externally pre-deleted object reconciles per §27 — and neither
    path loses submission metadata."""
    run = uuid.uuid4().hex[:10]
    honors_before = await snapshot_honor_ids(db_factory)
    teacher, student_id, headers, task_ids = await _seed_world(db_factory, run)
    storage = S3ObjectStorage(get_settings())
    submission_ids: list[UUID] = []
    try:
        # Two finalized submissions whose retention has come due: the
        # first keeps its object (a normal delete), the second loses it
        # BEFORE the scan (the §27 already-deleted shape). Separate
        # tasks: one non-terminal claim per user per task.
        for index in range(2):
            claim = await seed_claim(
                db_factory,
                task=await _task_fixture(db_factory, task_ids[index]),
                student_id=student_id,
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=_stack["app"]),
                base_url="http://e2e-retries",
            ) as client:
                intent_response = await client.post(
                    "/api/v1/submissions/upload-intent",
                    headers=headers,
                    json={
                        "claim_id": str(claim.claim_id),
                        "filename": f"清理重试{index}.csv",
                        "declared_type": FileType.CSV.value,
                        "size": len(_GOOD_CSV),
                    },
                )
                assert intent_response.status_code == 201, intent_response.text
                intent = intent_response.json()
                put_status = await asyncio.to_thread(
                    _put,
                    intent["upload_url"],
                    _GOOD_CSV,
                    intent["headers"],
                    len(_GOOD_CSV),
                )
                assert put_status == 200, put_status
                completed = await client.post(
                    "/api/v1/submissions/upload-complete",
                    headers=headers,
                    json={"intent_id": intent["intent_id"]},
                )
                assert completed.status_code == 200, completed.text
                submission_ids.append(uuid.UUID(completed.json()["id"]))

        metadata_before: dict[UUID, dict[str, Any]] = {}
        async with db_factory() as db:
            for submission_id in submission_ids:
                row = await db.get(Submission, submission_id)
                assert row is not None
                metadata_before[submission_id] = {
                    "object_key": row.object_key,
                    "original_filename": row.original_filename,
                    "file_size": row.file_size,
                    "declared_type": row.declared_type,
                    "validation_report": row.validation_report,
                }
                row.retention_until = datetime.now(UTC) - timedelta(hours=1)
            await db.commit()

        # The §27 shape: the second submission's object is already gone
        # (an external delete the scan must reconcile, not fail on).
        await asyncio.to_thread(
            storage.delete_object,
            object_key=metadata_before[submission_ids[1]]["object_key"],
        )

        first_scan = await asyncio.to_thread(
            cleanup_files_scan.run, f"e2e-retry-c1-{run}"
        )
        assert first_scan["deleted"] >= 1, first_scan
        assert first_scan["reconciled"] >= 1, first_scan

        # Both objects are gone and both rows are marked; the metadata
        # survived untouched (§13/§27: cleanup deletes objects, never
        # business facts).
        for submission_id in submission_ids:
            key = metadata_before[submission_id]["object_key"]
            assert (
                await asyncio.to_thread(storage.head_object, object_key=key) is None
            ), key

        async with db_factory() as db:
            for submission_id in submission_ids:
                row = await db.get(Submission, submission_id)
                assert row is not None
                assert row.deleted_at is not None
                snapshot = metadata_before[submission_id]
                assert row.object_key == snapshot["object_key"]
                assert row.original_filename == snapshot["original_filename"]
                assert row.file_size == snapshot["file_size"]
                assert row.declared_type == snapshot["declared_type"]
                assert row.validation_report == snapshot["validation_report"]

        # The duplicate job: the completed rows left the candidate set,
        # so nothing is re-deleted, re-marked, or disturbed.
        deleted_at_before = {}
        async with db_factory() as db:
            for submission_id in submission_ids:
                row = await db.get(Submission, submission_id)
                assert row is not None
                deleted_at_before[submission_id] = row.deleted_at

        second_scan = await asyncio.to_thread(
            cleanup_files_scan.run, f"e2e-retry-c2-{run}"
        )

        async with db_factory() as db:
            for submission_id in submission_ids:
                row = await db.get(Submission, submission_id)
                assert row is not None
                assert row.deleted_at == deleted_at_before[submission_id]
                snapshot = metadata_before[submission_id]
                assert row.original_filename == snapshot["original_filename"]
                assert row.validation_report == snapshot["validation_report"]
        # The duplicate scan did not re-touch our rows (their outcome
        # counters moved only for other rows, if any existed at all).
        assert second_scan["scanned"] >= 0
    finally:
        await _purge_objects(db_factory, task_ids)
        await clean_world(
            db_factory,
            user_ids=[teacher, student_id],
            task_ids=task_ids,
            honor_ids_before=honors_before,
        )


# --- case 5: crash between external call and finalize --------------------------------


async def test_crash_after_external_call_never_double_sends(
    db_factory: async_sessionmaker[AsyncSession],
    _stack: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that dies right after the provider accepted the
    message leaves the row SENDING; the stale-lease re-claim re-sends
    under the SAME provider idempotency key and the provider model
    records exactly ONE message."""
    run = uuid.uuid4().hex[:10]
    honors_before = await snapshot_honor_ids(db_factory)
    _teacher, student_id, headers, task_ids = await _seed_world(db_factory, run)

    crashing = _CrashingAfterRecordSmsSender()
    monkeypatch.setattr(
        "app.workers.jobs.send_notification.build_delivery_service",
        lambda **_: _patched_service(db_factory, crashing),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_stack["app"]),
            base_url="http://e2e-retries",
        ) as client:
            claimed = await client.post(
                f"/api/v1/tasks/{task_ids[0]}/claim", headers=headers
            )
            assert claimed.status_code == 201, claimed.text
            claim_id = uuid.UUID(claimed.json()["claim_id"])

        delivery = await _due_sms_delivery(db_factory, claim_id=claim_id)

        # The crash: the job body itself raises (the external message
        # was already recorded) — never a silent success.
        with pytest.raises(RuntimeError, match="simulated worker crash"):
            await _run_send_job(delivery.id, f"e2e-retry-crash-{run}")

        stuck = await _delivery_row(db_factory, delivery.id)
        assert stuck.status == DeliveryStatus.SENDING.value
        assert stuck.attempts == 1
        assert len(crashing.messages) == 1  # the provider holds one message

        # The re-run (the T8 stale-SENDING scan's enqueue shape): age
        # the claim past the lease threshold, swap in the healthy
        # provider, and re-fire the SAME job.
        healthy = _DedupingSmsSender()
        healthy.sent_keys = list(crashing.sent_keys)  # the provider remembers
        monkeypatch.setattr(
            "app.workers.jobs.send_notification.build_delivery_service",
            lambda **_: _patched_service(db_factory, healthy),
        )
        await _set_delivery(
            db_factory,
            delivery.id,
            updated_at=datetime.now(UTC) - timedelta(minutes=20),
        )
        retried = await _run_send_job(delivery.id, f"e2e-retry-crash2-{run}")

        assert retried["outcome"] == "SENT", retried
        expected_key = provider_idempotency_key(
            delivery.event_key, NotificationChannel.SMS, student_id
        )
        # The retry REALLY re-sent — under the SAME deterministic
        # idempotency key the crashed attempt used…
        assert healthy.calls == [expected_key], healthy.calls
        # …so the provider COLLAPSED it: no new external message, and
        # together with the crashed attempt's recording the provider
        # holds exactly ONE message for this delivery.
        assert healthy.messages == [], [m.idempotency_key for m in healthy.messages]
        assert len(crashing.messages) == 1
        assert crashing.messages[0].idempotency_key == expected_key

        row = await _delivery_row(db_factory, delivery.id)
        assert row.status == DeliveryStatus.SENT.value
        assert row.attempts == 2
    finally:
        await clean_world(
            db_factory,
            user_ids=[_teacher, student_id],
            task_ids=task_ids,
            honor_ids_before=honors_before,
        )
