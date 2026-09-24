# backend/tests/e2e/test_deadline_flows.py
"""Adverse deadline flows (plan 10 task 3): late submit, late review +
revision, invalidated empty-shell resubmit, and no-submit expiry with
reallocation.

Same real-wiring posture as ``test_happy_path`` (zero core provider
overrides, G18) with the one e2e-only seam plan 10 task 1 built: a
``SteppableClock`` behind ``get_business_clock`` so the FLOW hops (claim,
upload intent, finalize, review) read a stepped business instant, and
``trigger_expire_claims_scan`` running the REAL expiry job's async core
at the same explicit ``now`` (the Celery shell samples SystemClock by
design — the queue never owns deadline judgements). The validation job
entry runs with its production defaults: it re-reads the PERSISTED
``submitted_at``/deadlines, so the lock tier always reflects what the
API committed, never the worker's wall clock.

Reward-ladder verification for case 3 (the brief's required pre-assert
check): ``deadlines.reward_fraction`` places ``deadline + 4h <= t <
deadline + 12h`` at ``FRACTION_MID`` (50%), and
``RewardLockService.on_validation_passed`` only clamps to the 20% floor
on the RE-LOCK path when ``reward_fraction`` RAISES
``WindowClosedError`` (``submitted_at >= grace``, §11.3). A +7h
resubmission sits inside the 24h grace, so the re-lock takes the plain
50% tier — asserted below as tier 50, not the clamped 20.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.points.models import PointWallet
from app.modules.submissions.enums import FileType, ReviewStatus
from app.modules.submissions.models import (
    RewardLockHistory,
    Submission,
)
from app.modules.tasks.enums import (
    AssignmentAvailability,
    ClaimStatus,
    RewardLockStatus,
)
from app.modules.tasks.models import Assignment, AssignmentClaim
from app.workers.jobs.validate_submission import run_submission_validation
from tests.e2e.clock_control import (
    SteppableClock,
    install_clock_override,
    trigger_expire_claims_scan,
)
from tests.e2e.factories import (
    clean_world,
    seed_student,
    seed_task_with_assignments,
    seed_teacher_confirmed_totp,
    snapshot_honor_ids,
)
from tests.e2e.test_happy_path import _mint_access_token, _purge_objects, _put

pytestmark = pytest.mark.e2e

_BROKER_QUEUE_KEY = "celery"

#: A 60-minute RELATIVE task: deadline = claim + 1h, grace = +25h — the
#: four cases are small, exact clock steps from there.
_DURATION_MINUTES = 60
_T0 = datetime(2026, 9, 21, 8, 0, tzinfo=UTC)

_GOOD_CSV = (
    b"url,title\n"
    b"https://example.com/note/1,\xe7\xac\xac\xe4\xb8\x80\xe6\x9d\xa1\n"
    b"https://example.com/note/2,\xe7\xac\xac\xe4\xba\x8c\xe6\x9d\xa1\n"
)

#: The empty shell: schema-legal rows (unique urls, non-null string
#: titles — the columns are NOT nullable, so truly blank cells would
#: fail the STRUCTURAL gate) carrying placeholder content: the machine
#: gate passes and only the human review judges it worthless (plan task
#: 3 step 3's "machine-pass shell").
_SHELL_CSV = (
    b"url,title\n"
    b"https://example.com/shell/1,placeholder\n"
    b"https://example.com/shell/2,placeholder\n"
)


class _World:
    """One deadline-flow run's seeded ids and API handles."""

    def __init__(self, run: str) -> None:
        self.run = run
        self.user_ids: list[UUID] = []
        self.task_ids: list[UUID] = []
        self.honors_before: set[UUID] | None = None


@pytest_asyncio.fixture
async def _stack() -> AsyncIterator[dict[str, Any]]:
    """App + stepped clock + broker; the process-wide singleton resets
    live in the e2e conftest (autouse) — teardown here only restores the
    broker queue."""
    app = create_app()
    clock = SteppableClock(_T0)
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


async def _seed_world(
    db_factory: async_sessionmaker[AsyncSession],
    world: _World,
    student_count: int,
    *,
    assignment_count: int = 2,
) -> tuple[dict[str, str], dict[str, dict[str, str]], str]:
    """Teacher + N students + one 60-minute task; returns the teacher and
    per-student bearer headers plus the task id. The expiry case passes
    ``assignment_count=1`` so the reallocating claim can only land on the
    released unit."""
    teacher = await seed_teacher_confirmed_totp(db_factory, run=world.run)
    world.user_ids.append(teacher.user_id)
    # Distinct run PREFIXES (not suffixes): the factories' phone recipe
    # derives from run[:8], so "0<run>" and "1<run>" stay collision-free.
    students = [
        await seed_student(db_factory, run=f"{index}{world.run}")
        for index in range(student_count)
    ]
    world.user_ids.extend(student.user_id for student in students)
    task = await seed_task_with_assignments(
        db_factory,
        teacher_id=teacher.user_id,
        run=world.run,
        assignment_count=assignment_count,
        duration_minutes=_DURATION_MINUTES,
    )
    world.task_ids.append(task.task_id)
    world.honors_before = await snapshot_honor_ids(db_factory)
    teacher_token = await _mint_access_token(db_factory, teacher.user_id)
    teacher_headers = {"Authorization": f"Bearer {teacher_token}"}
    student_headers = {
        student.username: {
            "Authorization": (
                f"Bearer {await _mint_access_token(db_factory, student.user_id)}"
            )
        }
        for student in students
    }
    return teacher_headers, student_headers, str(task.task_id)


async def _claim(
    client: httpx.AsyncClient, headers: dict[str, str], task_id: str
) -> str:
    response = await client.post(f"/api/v1/tasks/{task_id}/claim", headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["claim_id"]


async def _submit(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    claim_id: str,
    body: bytes,
    *,
    filename: str,
) -> str:
    """The real presign -> PUT -> finalize chain; returns the submission
    id. The intent/finalize routes read the STEPPED clock, so the
    persisted ``submitted_at`` is the instant the caller advanced to."""
    intent_response = await client.post(
        "/api/v1/submissions/upload-intent",
        headers=headers,
        json={
            "claim_id": claim_id,
            "filename": filename,
            "declared_type": FileType.CSV.value,
            "size": len(body),
        },
    )
    assert intent_response.status_code == 201, intent_response.text
    intent = intent_response.json()
    put_status = await asyncio.to_thread(
        _put, intent["upload_url"], body, intent["headers"], len(body)
    )
    assert put_status == 200, put_status
    completed = await client.post(
        "/api/v1/submissions/upload-complete",
        headers=headers,
        json={"intent_id": intent["intent_id"]},
    )
    assert completed.status_code == 200, completed.text
    return completed.json()["id"]


async def _validate(submission_id: str, run: str) -> dict[str, Any]:
    result = await asyncio.to_thread(
        run_submission_validation,
        submission_id,
        request_id=f"e2e-deadline-{run}",
    )
    assert result["validation_status"] == "VALIDATED", result
    return result


async def _lock_history(
    db_factory: async_sessionmaker[AsyncSession], claim_id: UUID
) -> list[tuple[str, str, int | None]]:
    async with db_factory() as db:
        rows = (
            (
                await db.execute(
                    select(RewardLockHistory)
                    .where(RewardLockHistory.claim_id == claim_id)
                    .order_by(RewardLockHistory.created_at)
                )
            )
            .scalars()
            .all()
        )
    return [
        (row.lock_status_from, row.lock_status_to, row.reward_tier_locked)
        for row in rows
    ]


async def test_plus_2h_late_submit_locks_80_percent_tier(
    db_factory: async_sessionmaker[AsyncSession], _stack: dict[str, Any]
) -> None:
    """Deadline + 2h submit -> PROVISIONAL tier 80; approval grants the
    exact integer 80 (100 base x 0.8, Decimal floor)."""
    clock: SteppableClock = _stack["clock"]
    world = _World(uuid.uuid4().hex[:12])
    teacher_headers, student_headers, task_id = await _seed_world(db_factory, world, 1)
    (student_header,) = student_headers.values()
    try:
        transport = httpx.ASGITransport(app=_stack["app"])
        async with httpx.AsyncClient(
            transport=transport, base_url="http://e2e-deadline"
        ) as client:
            claim_id = await _claim(client, student_header, task_id)

            clock.advance(timedelta(hours=3))  # deadline (+1h) + 2h late
            submission_id = await _submit(
                client, student_header, claim_id, _GOOD_CSV, filename="迟交.csv"
            )
            await _validate(submission_id, world.run)

            approved = await client.post(
                f"/api/v1/teacher/submissions/{submission_id}/approve",
                headers=teacher_headers,
            )
            assert approved.status_code == 200, approved.text
            decision = approved.json()
            assert decision["claim_status"] == ClaimStatus.COMPLETED.value
            assert decision["reward_lock_status"] == RewardLockStatus.CONFIRMED.value
            assert decision["points_granted"] == 80  # exact integer, no float

        claim_uuid = uuid.UUID(claim_id)
        assert await _lock_history(db_factory, claim_uuid) == [
            (RewardLockStatus.NONE.value, RewardLockStatus.PROVISIONAL.value, 80),
            (RewardLockStatus.PROVISIONAL.value, RewardLockStatus.CONFIRMED.value, 80),
        ]
        async with db_factory() as db:
            student_id = world.user_ids[1]
            claim_row = await db.get(AssignmentClaim, claim_uuid)
            assert claim_row is not None
            assert claim_row.reward_tier_locked == 80
            assert claim_row.locked_reward_points == 80
            wallet = await db.get(PointWallet, student_id)
            assert wallet is not None
            assert wallet.available_points == 80
            assert wallet.earned_points == 80
    finally:
        await _purge_objects(db_factory, world.task_ids)
        # The real review routes appended audit rows with this run's
        # teacher actor; audit_logs carry no FKs, so the sweep is
        # explicit (the same discipline as the happy path's teardown).
        async with db_factory() as db:
            await db.execute(
                delete(AuditLog).where(AuditLog.actor_user_id == world.user_ids[0])
            )
            await db.commit()
        await clean_world(
            db_factory,
            user_ids=world.user_ids,
            task_ids=world.task_ids,
            honor_ids_before=world.honors_before,
        )


async def test_late_teacher_review_extends_revision_window_keeps_100(
    db_factory: async_sessionmaker[AsyncSession], _stack: dict[str, Any]
) -> None:
    """On-time valid submit; Teacher reviews two days later; the §11.4
    window is >= review + 24h; a revised approval keeps the 100% lock."""
    clock: SteppableClock = _stack["clock"]
    world = _World(uuid.uuid4().hex[:12])
    teacher_headers, student_headers, task_id = await _seed_world(db_factory, world, 1)
    (student_header,) = student_headers.values()
    try:
        transport = httpx.ASGITransport(app=_stack["app"])
        async with httpx.AsyncClient(
            transport=transport, base_url="http://e2e-deadline"
        ) as client:
            claim_id = await _claim(client, student_header, task_id)

            clock.advance(timedelta(minutes=30))  # on time (deadline +1h)
            first_submission = await _submit(
                client, student_header, claim_id, _GOOD_CSV, filename="初版.csv"
            )
            await _validate(first_submission, world.run)

            clock.advance(timedelta(days=2) - timedelta(minutes=30))  # T0+2d
            review_instant = clock.now()
            required = await client.post(
                f"/api/v1/teacher/submissions/{first_submission}/revision-required",
                headers=teacher_headers,
                json={"note": "补齐第三条数据后重新提交。"},
            )
            assert required.status_code == 200, required.text
            revision = required.json()
            assert revision["claim_status"] == ClaimStatus.REVISION_REQUIRED.value
            assert revision["reward_lock_status"] == RewardLockStatus.PROVISIONAL.value
            revision_deadline = datetime.fromisoformat(revision["revision_deadline_at"])
            assert revision_deadline >= review_instant + timedelta(hours=24)

            clock.advance(timedelta(hours=1))  # revise within the window
            second_submission = await _submit(
                client, student_header, claim_id, _GOOD_CSV, filename="修订版.csv"
            )
            await _validate(second_submission, world.run)

            approved = await client.post(
                f"/api/v1/teacher/submissions/{second_submission}/approve",
                headers=teacher_headers,
            )
            assert approved.status_code == 200, approved.text
            decision = approved.json()
            assert decision["points_granted"] == 100  # the lock SURVIVED revision

        async with db_factory() as db:
            second_row = await db.get(Submission, uuid.UUID(second_submission))
            assert second_row is not None
            assert second_row.version == 2
            assert second_row.review_status == ReviewStatus.APPROVED.value
        assert await _lock_history(db_factory, uuid.UUID(claim_id)) == [
            (RewardLockStatus.NONE.value, RewardLockStatus.PROVISIONAL.value, 100),
            (RewardLockStatus.PROVISIONAL.value, RewardLockStatus.CONFIRMED.value, 100),
        ]
    finally:
        await _purge_objects(db_factory, world.task_ids)
        # The real review routes appended audit rows with this run's
        # teacher actor; audit_logs carry no FKs, so the sweep is
        # explicit (the same discipline as the happy path's teardown).
        async with db_factory() as db:
            await db.execute(
                delete(AuditLog).where(AuditLog.actor_user_id == world.user_ids[0])
            )
            await db.commit()
        await clean_world(
            db_factory,
            user_ids=world.user_ids,
            task_ids=world.task_ids,
            honor_ids_before=world.honors_before,
        )


async def test_invalidated_shell_resubmit_plus_7h_locks_50_percent(
    db_factory: async_sessionmaker[AsyncSession], _stack: dict[str, Any]
) -> None:
    """On-time machine-pass shell -> invalidate -> valid resubmit at
    deadline + 7h -> re-lock at the 50% tier (inside the 24h grace: the
    §11.3 20% clamp only fires PAST grace on the re-lock path)."""
    clock: SteppableClock = _stack["clock"]
    world = _World(uuid.uuid4().hex[:12])
    teacher_headers, student_headers, task_id = await _seed_world(db_factory, world, 1)
    (student_header,) = student_headers.values()
    try:
        transport = httpx.ASGITransport(app=_stack["app"])
        async with httpx.AsyncClient(
            transport=transport, base_url="http://e2e-deadline"
        ) as client:
            claim_id = await _claim(client, student_header, task_id)

            clock.advance(timedelta(minutes=30))  # on-time shell
            shell_submission = await _submit(
                client, student_header, claim_id, _SHELL_CSV, filename="空壳.csv"
            )
            shell_result = await _validate(shell_submission, world.run)
            assert shell_result["passed"] is True  # structural gate passes

            invalidated = await client.post(
                f"/api/v1/teacher/submissions/{shell_submission}/invalidate-reward-lock",
                headers=teacher_headers,
                json={"reason": "空壳数据：占位内容无采集价值，判无效。"},
            )
            assert invalidated.status_code == 200, invalidated.text
            body = invalidated.json()
            assert body["claim_status"] == ClaimStatus.REVISION_REQUIRED.value
            assert body["reward_lock_status"] == RewardLockStatus.INVALIDATED.value

            clock.advance(timedelta(hours=7, minutes=30))  # deadline (+1h) + 7h
            valid_submission = await _submit(
                client, student_header, claim_id, _GOOD_CSV, filename="有效重交.csv"
            )
            await _validate(valid_submission, world.run)

            approved = await client.post(
                f"/api/v1/teacher/submissions/{valid_submission}/approve",
                headers=teacher_headers,
            )
            assert approved.status_code == 200, approved.text
            decision = approved.json()
            assert decision["points_granted"] == 50  # 50% tier, NOT the 20% clamp

        async with db_factory() as db:
            claim_row = await db.get(AssignmentClaim, uuid.UUID(claim_id))
            assert claim_row is not None
            assert claim_row.reward_tier_locked == 50
            assert claim_row.locked_reward_points == 50
            wallet = await db.get(PointWallet, world.user_ids[1])
            assert wallet is not None
            assert wallet.earned_points == 50
        assert await _lock_history(db_factory, uuid.UUID(claim_id)) == [
            (RewardLockStatus.NONE.value, RewardLockStatus.PROVISIONAL.value, 100),
            (
                RewardLockStatus.PROVISIONAL.value,
                RewardLockStatus.INVALIDATED.value,
                100,
            ),
            (
                RewardLockStatus.INVALIDATED.value,
                RewardLockStatus.PROVISIONAL.value,
                50,
            ),
            (RewardLockStatus.PROVISIONAL.value, RewardLockStatus.CONFIRMED.value, 50),
        ]
    finally:
        await _purge_objects(db_factory, world.task_ids)
        # The real review routes appended audit rows with this run's
        # teacher actor; audit_logs carry no FKs, so the sweep is
        # explicit (the same discipline as the happy path's teardown).
        async with db_factory() as db:
            await db.execute(
                delete(AuditLog).where(AuditLog.actor_user_id == world.user_ids[0])
            )
            await db.commit()
        await clean_world(
            db_factory,
            user_ids=world.user_ids,
            task_ids=world.task_ids,
            honor_ids_before=world.honors_before,
        )


async def test_no_submit_expiry_releases_assignment_for_another_student(
    db_factory: async_sessionmaker[AsyncSession], _stack: dict[str, Any]
) -> None:
    """Grace passes with no submission -> the REAL expiry job marks the
    claim EXPIRED, the assignment returns to AVAILABLE, and a DIFFERENT
    student claims it."""
    clock: SteppableClock = _stack["clock"]
    world = _World(uuid.uuid4().hex[:12])
    teacher_headers, student_headers, task_id = await _seed_world(
        db_factory, world, 2, assignment_count=1
    )
    usernames = list(student_headers)
    try:
        transport = httpx.ASGITransport(app=_stack["app"])
        async with httpx.AsyncClient(
            transport=transport, base_url="http://e2e-deadline"
        ) as client:
            claim_id = await _claim(client, student_headers[usernames[0]], task_id)
            assignment_id = None
            async with db_factory() as db:
                claim_row = await db.get(AssignmentClaim, uuid.UUID(claim_id))
                assert claim_row is not None
                assignment_id = claim_row.assignment_id

            # Past grace (deadline +1h, grace +25h): the real scan runs at
            # the same stepped instant the API would read.
            clock.advance(timedelta(hours=25, minutes=1))
            payloads = await trigger_expire_claims_scan(clock.now())
            outcomes = {entry.get("claim_id"): entry["outcome"] for entry in payloads}
            assert outcomes.get(claim_id) == "EXPIRED", outcomes.get(claim_id)

            # The released unit is back on the shelf before anyone claims.
            async with db_factory() as db:
                assignment_row = await db.get(Assignment, assignment_id)
                assert assignment_row is not None
                assert (
                    assignment_row.availability_status
                    == AssignmentAvailability.AVAILABLE.value
                )

            second_claim = await _claim(client, student_headers[usernames[1]], task_id)
            assert second_claim != claim_id

        async with db_factory() as db:
            expired = await db.get(AssignmentClaim, uuid.UUID(claim_id))
            assert expired is not None
            assert expired.status == ClaimStatus.EXPIRED.value
            assert expired.terminal_at is not None
            second = await db.get(AssignmentClaim, uuid.UUID(second_claim))
            assert second is not None
            assert second.status == ClaimStatus.CLAIMED.value
            assert second.user_id == world.user_ids[2]
            # The released unit is exactly what the second student got.
            assert second.assignment_id == assignment_id
    finally:
        await _purge_objects(db_factory, world.task_ids)
        # The real review routes appended audit rows with this run's
        # teacher actor; audit_logs carry no FKs, so the sweep is
        # explicit (the same discipline as the happy path's teardown).
        async with db_factory() as db:
            await db.execute(
                delete(AuditLog).where(AuditLog.actor_user_id == world.user_ids[0])
            )
            await db.commit()
        await clean_world(
            db_factory,
            user_ids=world.user_ids,
            task_ids=world.task_ids,
            honor_ids_before=world.honors_before,
        )
