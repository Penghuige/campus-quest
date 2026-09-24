# backend/tests/e2e/test_happy_path.py
"""Full-chain happy path over REAL wiring only (plan 10 task 2, step 1).

The plan's step-1 chain, every hop through the public surface with ZERO
core provider overrides (G18 — the composition-smoke posture, widened
from one submission to the whole product loop):

    whitelist -> phone OTP (real Redis hash) -> register -> login
    -> Teacher publishes Task/Assignments (real management API)
    -> Student claims -> presign -> real HTTP PUT (+ 412 replay)
    -> finalize -> real validation job -> Teacher approves
    -> one ASSIGNMENT_REWARD ledger + wallet + Redis ranking projection
    -> redeem -> Admin approves (consume) -> Admin fulfills

World-building stays ORM-side (the e1 factories' charter): the teacher,
the admin (with the management guard's confirmed-TOTP row), the reward
item, and the whitelist entry are seeded directly; every FLOW step above
runs through ``create_app()``'s real routes, the real presigning
``S3ObjectStorage``, the real ``run_submission_validation`` job entry
(downloading the object this test PUT back from MinIO into the sandboxed
validator), and the real ``run_ranking_projection`` job entry.

The OTP answer uses the ruled "test-side direct read" (tests/e2e/
otp_probe.py): the production service persists only the HMAC digest, so
the probe recovers the 6-digit code from the real Redis record with the
same settings secret — challenge creation, at-rest hashing, and the
verify route all stay production behavior.

Committed NullPool harness + FK-ordered ``clean_world`` teardown + the
S3 object purge + broker restore + singleton resets: the e1 conftest /
composition-smoke discipline, unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import urllib.error
import urllib.request
import uuid
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import SystemClock
from app.core.config import get_settings
from app.integrations.object_storage_s3 import S3ObjectStorage
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.identity.dependencies import get_access_token_codec
from app.modules.identity.models import User
from app.modules.identity.session_service import SessionService
from app.modules.notifications.models import Notification
from app.modules.points.enums import LedgerType, ReservationStatus
from app.modules.points.models import (
    PointReservation,
    PointsLedger,
    PointWallet,
    RewardRedemption,
)
from app.modules.rankings.periods import (
    business_day,
    business_month,
    daily_key,
    monthly_key,
)
from app.modules.rankings.redis_projection import ALL_TIME_KEY
from app.modules.submissions.enums import FileType, ReviewStatus, ValidationStatus
from app.modules.submissions.models import (
    RewardLockHistory,
    Submission,
    UploadIntent,
)
from app.modules.tasks.enums import ClaimStatus, RewardLockStatus
from app.modules.tasks.models import AssignmentClaim
from app.workers.jobs.project_ranking_update import run_ranking_projection
from app.workers.jobs.validate_submission import run_submission_validation
from tests.e2e.factories import (
    CSV_SUBMISSION_SCHEMA,
    clean_world,
    seed_admin_confirmed_totp,
    seed_reward_item,
    seed_teacher_confirmed_totp,
    seed_whitelist_entry,
    snapshot_honor_ids,
)
from tests.e2e.otp_probe import recover_otp_code

pytestmark = pytest.mark.e2e

_BROKER_QUEUE_KEY = "celery"

# A legal CSV against the factories' schema (url unique string + title
# string): two data rows, no findings -> the machine gate passes.
_CSV_BODY = (
    b"url,title\n"
    b"https://example.com/note/1,\xe7\xac\xac\xe4\xb8\x80\xe6\x9d\xa1\n"
    b"https://example.com/note/2,\xe7\xac\xac\xe4\xba\x8c\xe6\x9d\xa1\n"
)

_CLIENT_HEADERS = {"If-None-Match": "*", "Content-Type": "text/csv"}

_PASSWORD = "correct-horse-battery-e2e"


def _unique_phone(run: str) -> str:
    """Run-unique E.164 mobile number (the factories' deterministic
    recipe, repeated locally so the module owns its flow inputs)."""
    digits = str(int(run[:8], 16) % 10**8).zfill(8)
    return f"+86138{digits}"


def _unique_student_number(run: str) -> str:
    """Run-unique numeric student number (6-20 ASCII digits)."""
    return f"20{int(run, 16) % 10**15}"


def _put(
    url: str, body: bytes, client_headers: dict[str, str], pinned_content_length: int
) -> int:
    """Real HTTP PUT through the presigned URL exactly like a compliant
    client: the adapter-returned echo headers verbatim, plus
    Content-Length framed to the pinned byte count (the composition-
    smoke stand-in for the browser's automatic framing)."""
    headers = {**client_headers, "Content-Length": str(pinned_content_length)}
    request = urllib.request.Request(url, data=body, method="PUT", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


async def _mint_access_token(
    factory: async_sessionmaker[AsyncSession], user_id: UUID
) -> str:
    """A real access token through the real ``SessionService`` (the
    composition-smoke seeding seam): real codec, real session row, so
    the role guards verify exactly as in production."""
    async with factory() as db:
        user = await db.get(User, user_id)
        assert user is not None
        sessions = SessionService(
            clock=SystemClock(), access_codec=get_access_token_codec()
        )
        _, tokens = await sessions.issue_session(db, user=user, now=SystemClock().now())
        await db.commit()
        return tokens.access_token


async def _purge_objects(
    factory: async_sessionmaker[AsyncSession], task_ids: list[UUID]
) -> None:
    """Delete the objects this run PUT, through the real adapter (§27:
    an absent object raises, so each delete is guarded)."""
    storage = S3ObjectStorage(get_settings())
    async with factory() as db:
        keys = (
            (
                await db.execute(
                    select(UploadIntent.object_key).where(
                        UploadIntent.claim_id.in_(
                            select(AssignmentClaim.id).where(
                                AssignmentClaim.task_id.in_(task_ids)
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


@pytest_asyncio.fixture
async def _stack() -> AsyncIterator[dict[str, Any]]:
    """The real composition stack: the app under lifespan plus the broker
    client the queue-depth and projection asserts read. The process-wide
    singleton resets live in the e2e conftest (autouse), so teardown here
    only restores the broker queue this run published onto."""
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


async def test_full_happy_path_chain(
    db_factory: async_sessionmaker[AsyncSession], _stack: dict[str, Any]
) -> None:
    """The whole plan-step-1 chain, asserting every state and
    ledger/reservation transition along the way."""
    settings = _stack["settings"]
    broker: aioredis.Redis = _stack["broker"]
    run = uuid.uuid4().hex[:12]
    honors_before = await snapshot_honor_ids(db_factory)

    teacher = await seed_teacher_confirmed_totp(db_factory, run=run)
    admin = await seed_admin_confirmed_totp(db_factory, run=run)
    item = await seed_reward_item(db_factory, run=run, point_cost=50)
    student_number = _unique_student_number(run)
    phone = _unique_phone(run)
    await seed_whitelist_entry(db_factory, student_number=student_number)

    teacher_token = await _mint_access_token(db_factory, teacher.user_id)
    admin_token = await _mint_access_token(db_factory, admin.user_id)
    teacher_headers = {"Authorization": f"Bearer {teacher_token}"}
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    task_id: str | None = None
    student_id: UUID | None = None
    claim_id: str | None = None
    submission_id: str | None = None
    redemption_id: str | None = None
    try:
        queue_depth_before = await broker.llen(_BROKER_QUEUE_KEY)
        transport = httpx.ASGITransport(app=_stack["app"])
        async with httpx.AsyncClient(
            transport=transport, base_url="http://e2e-happy"
        ) as client:
            # --- identity: real OTP -> register -> login ------------------
            challenged = await client.post(
                "/api/v1/auth/phone/challenges", json={"phone": phone}
            )
            assert challenged.status_code == 200, challenged.text
            challenge = challenged.json()
            live_challenge_id, code = await recover_otp_code(broker, phone)
            assert str(live_challenge_id) == challenge["challenge_id"]
            verified = await client.post(
                f"/api/v1/auth/phone/challenges/{live_challenge_id}/verify",
                json={"code": code},
            )
            assert verified.status_code == 200, verified.text
            phone_token = verified.json()["phone_token"]

            registered = await client.post(
                "/api/v1/auth/register",
                json={
                    "student_number": student_number,
                    "nickname": f"全链同学{run[:4]}",
                    "phone_token": phone_token,
                    "password": _PASSWORD,
                },
            )
            assert registered.status_code == 201, registered.text
            student_id = uuid.UUID(registered.json()["id"])

            logged_in = await client.post(
                "/api/v1/auth/login",
                json={"username": student_number, "password": _PASSWORD},
            )
            assert logged_in.status_code == 200, logged_in.text
            student_headers = {
                "Authorization": f"Bearer {logged_in.json()['access_token']}"
            }

            # --- teacher publishes Task + Assignments (real mgmt API) -----
            created = await client.post(
                "/api/v1/teacher/tasks",
                headers=teacher_headers,
                json={
                    "title": f"全链采集任务{run[:6]}",
                    "description": "全链 happy-path 的采集任务。",
                    "base_reward_points": 100,
                    "deadline_mode": "RELATIVE",
                    "allowed_file_types": ["CSV"],
                    "max_file_size_bytes": 10 * 1024 * 1024,
                    "duration_minutes": 4320,
                    "submission_schema": CSV_SUBMISSION_SCHEMA,
                    "submission_schema_version": 1,
                    "notification_channels": ["SMS"],
                },
            )
            assert created.status_code == 201, created.text
            task_id = created.json()["id"]

            import_csv = (
                "platform,keyword\n"
                f"xiaohongshu,全链{run[:4]}1\n"
                f"xiaohongshu,全链{run[:4]}2\n"
                f"xiaohongshu,全链{run[:4]}3\n"
            ).encode()
            previewed = await client.post(
                f"/api/v1/teacher/tasks/{task_id}/assignments/import/preview",
                headers={**teacher_headers, "Content-Type": "text/csv"},
                content=import_csv,
            )
            assert previewed.status_code == 200, previewed.text
            preview = previewed.json()
            assert preview["valid_count"] == 3
            assert preview["error_count"] == 0
            confirmed_import = await client.post(
                f"/api/v1/teacher/tasks/{task_id}/assignments/import/confirm",
                headers=teacher_headers,
                json={"preview_token": preview["preview_token"]},
            )
            assert confirmed_import.status_code == 200, confirmed_import.text
            assert confirmed_import.json()["inserted"] == 3

            published = await client.post(
                f"/api/v1/teacher/tasks/{task_id}/publish", headers=teacher_headers
            )
            assert published.status_code == 200, published.text
            assert published.json()["status"] == "PUBLISHED"
            assert published.json()["claimable"] is True

            # --- student claims (random server-side allocation) -----------
            claimed = await client.post(
                f"/api/v1/tasks/{task_id}/claim", headers=student_headers
            )
            assert claimed.status_code == 201, claimed.text
            claim = claimed.json()
            claim_id = claim["claim_id"]
            assert claim["status"] == ClaimStatus.CLAIMED.value
            assert claim["base_reward_points_snapshot"] == 100

            # --- upload chain: real presign -> real PUT -> finalize -------
            intent_response = await client.post(
                "/api/v1/submissions/upload-intent",
                headers=student_headers,
                json={
                    "claim_id": claim_id,
                    "filename": "全链结果.csv",
                    "declared_type": FileType.CSV.value,
                    "size": len(_CSV_BODY),
                },
            )
            assert intent_response.status_code == 201, intent_response.text
            intent = intent_response.json()
            assert intent["headers"] == _CLIENT_HEADERS, intent["headers"]
            assert intent["pinned_content_length"] == len(_CSV_BODY)
            assert intent["upload_url"].startswith(
                f"{settings.s3_endpoint_url}/{settings.s3_bucket}/"
            )

            put_status = await asyncio.to_thread(
                _put,
                intent["upload_url"],
                _CSV_BODY,
                intent["headers"],
                intent["pinned_content_length"],
            )
            assert put_status == 200
            replay_status = await asyncio.to_thread(
                _put,
                intent["upload_url"],
                _CSV_BODY,
                intent["headers"],
                intent["pinned_content_length"],
            )
            assert replay_status == 412

            completed = await client.post(
                "/api/v1/submissions/upload-complete",
                headers=student_headers,
                json={"intent_id": intent["intent_id"]},
            )
            assert completed.status_code == 200, completed.text
            submission = completed.json()
            submission_id = submission["id"]
            assert submission["version"] == 1
            assert submission["file_size"] == len(_CSV_BODY)
            assert submission["validation_status"] == ValidationStatus.UPLOADED.value
            assert submission["review_status"] == ReviewStatus.PENDING_REVIEW.value
            assert await broker.llen(_BROKER_QUEUE_KEY) >= queue_depth_before + 1

            # --- the real validation job (own engine, real MinIO read) ----
            job_result = await asyncio.to_thread(
                run_submission_validation,
                submission_id,
                request_id=f"e2e-happy-{run}",
            )
            assert job_result["validation_status"] == "VALIDATED"
            assert job_result["passed"] is True
            assert job_result["row_count"] == 2

            # --- teacher approves: lock CONFIRMED, grant, projections -----
            approved = await client.post(
                f"/api/v1/teacher/submissions/{submission_id}/approve",
                headers=teacher_headers,
            )
            assert approved.status_code == 200, approved.text
            decision = approved.json()
            assert decision["claim_status"] == ClaimStatus.COMPLETED.value
            assert decision["reward_lock_status"] == RewardLockStatus.CONFIRMED.value
            assert decision["points_granted"] == 100
            assert decision["already_reviewed"] is False
            replayed = await client.post(
                f"/api/v1/teacher/submissions/{submission_id}/approve",
                headers=teacher_headers,
            )
            assert replayed.status_code == 200
            assert replayed.json()["already_reviewed"] is True

        # The ranking projection job core at the ledger's own effective
        # instant (read from the committed entry, not assumed).
        async with db_factory() as db:
            reward_entry = await db.scalar(
                select(PointsLedger).where(
                    PointsLedger.user_id == student_id,
                    PointsLedger.ledger_type == LedgerType.ASSIGNMENT_REWARD.value,
                )
            )
        assert reward_entry is not None
        projection = await asyncio.to_thread(
            run_ranking_projection,
            str(student_id),
            reward_entry.ranking_effective_at.isoformat(),
            request_id=f"e2e-happy-rank-{run}",
        )
        tz = ZoneInfo(settings.business_timezone)
        expected_keys = {
            daily_key(business_day(reward_entry.ranking_effective_at, tz)),
            monthly_key(business_month(reward_entry.ranking_effective_at, tz)),
            ALL_TIME_KEY,
        }
        assert set(projection["updated_keys"]) == expected_keys
        for key in expected_keys:
            score = await broker.zscore(key, str(student_id))
            assert score == 100.0, (key, score)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_stack["app"]),
            base_url="http://e2e-happy",
        ) as client:
            wallet_view = await client.get("/api/v1/points/me", headers=student_headers)
            assert wallet_view.status_code == 200, wallet_view.text
            wallet = wallet_view.json()
            assert wallet["available_points"] == 100
            assert wallet["earned_points"] == 100
            assert wallet["spendable_points"] == 100
            assert wallet["point_debt"] == 0

            # --- redeem -> admin approve (consume) -> admin fulfill -------
            redeemed = await client.post(
                f"/api/v1/rewards/{item.reward_item_id}/redeem",
                headers=student_headers,
            )
            assert redeemed.status_code == 201, redeemed.text
            redemption = redeemed.json()
            redemption_id = redemption["id"]
            assert redemption["status"] == "REQUESTED"
            assert redemption["points"] == 50

            frozen_wallet = await client.get(
                "/api/v1/points/me", headers=student_headers
            )
            frozen = frozen_wallet.json()
            assert frozen["available_points"] == 100  # freeze, not spend
            assert frozen["spendable_points"] == 50

            approved_redemption = await client.post(
                f"/api/v1/teacher/rewards/redemptions/{redemption_id}/approve",
                headers=admin_headers,
            )
            assert approved_redemption.status_code == 200, approved_redemption.text
            assert approved_redemption.json()["status"] == "APPROVED"

            fulfilled_redemption = await client.post(
                f"/api/v1/teacher/rewards/redemptions/{redemption_id}/fulfill",
                headers=admin_headers,
                json={"note": "全链 happy-path 发放"},
            )
            assert fulfilled_redemption.status_code == 200, fulfilled_redemption.text
            assert fulfilled_redemption.json()["status"] == "FULFILLED"

            spent_wallet = await client.get(
                "/api/v1/points/me", headers=student_headers
            )
            spent = spent_wallet.json()
            assert spent["available_points"] == 50
            assert spent["earned_points"] == 100  # spending never touches earned
            assert spent["spendable_points"] == 50

        # --- end state straight against PostgreSQL ------------------------
        async with db_factory() as db:
            submission_row = await db.get(Submission, uuid.UUID(submission_id))
            assert submission_row is not None
            assert submission_row.validation_status == ValidationStatus.VALIDATED.value
            assert submission_row.review_status == ReviewStatus.APPROVED.value

            claim_row = await db.get(AssignmentClaim, uuid.UUID(claim_id))
            assert claim_row is not None
            assert claim_row.status == ClaimStatus.COMPLETED.value
            assert claim_row.terminal_at is not None
            assert claim_row.reward_lock_status == RewardLockStatus.CONFIRMED.value
            assert claim_row.reward_tier_locked == 100
            assert claim_row.locked_reward_points == 100
            assert claim_row.latest_submission_id == submission_row.id

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
                (row.lock_status_from, row.lock_status_to) for row in lock_history
            ] == [
                (RewardLockStatus.NONE.value, RewardLockStatus.PROVISIONAL.value),
                (RewardLockStatus.PROVISIONAL.value, RewardLockStatus.CONFIRMED.value),
            ]

            ledger_entries = (
                (
                    await db.execute(
                        select(PointsLedger)
                        .where(PointsLedger.user_id == student_id)
                        .order_by(PointsLedger.created_at)
                    )
                )
                .scalars()
                .all()
            )
            assert [(entry.ledger_type, entry.amount) for entry in ledger_entries] == [
                (LedgerType.ASSIGNMENT_REWARD.value, 100),
                (LedgerType.REWARD_REDEMPTION.value, -50),
            ]
            assert ledger_entries[0].source_id == claim_row.id
            assert ledger_entries[0].affects_balance is True
            assert ledger_entries[0].affects_ranking is True
            assert ledger_entries[1].affects_ranking is False

            wallet_row = await db.get(PointWallet, student_id)
            assert wallet_row is not None
            assert wallet_row.available_points == 50
            assert wallet_row.earned_points == 100

            reservation_rows = (
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
            assert len(reservation_rows) == 1
            assert reservation_rows[0].points == 50
            assert (
                reservation_rows[0].status == ReservationStatus.CONSUMED.value
            )  # ACTIVE -> CONSUMED on the admin approve

            redemption_row = await db.get(RewardRedemption, uuid.UUID(redemption_id))
            assert redemption_row is not None
            assert redemption_row.status == "FULFILLED"
            assert redemption_row.decided_by == admin.user_id
            assert redemption_row.fulfilled_at is not None

            notifications = (
                (
                    await db.execute(
                        select(Notification.event_type).where(
                            Notification.user_id == student_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert "SUBMISSION_APPROVED" in notifications
    finally:
        # Clean even from a partial run: only the ids actually created
        # ride along, so an early failure still leaves a clean database
        # (the original error, not a teardown assert, stays the headline).
        await _purge_objects(db_factory, [uuid.UUID(task_id)] if task_id else [])
        async with db_factory() as db:
            # The real review/redemption routes appended audit rows with
            # this run's staff actors; audit_logs carry no FKs, so the
            # sweep is explicit (nothing else owns their removal).
            await db.execute(
                delete(AuditLog).where(
                    AuditLog.actor_user_id.in_([teacher.user_id, admin.user_id])
                )
            )
            await db.commit()
        await clean_world(
            db_factory,
            user_ids=[
                teacher.user_id,
                admin.user_id,
                *([student_id] if student_id else []),
            ],
            task_ids=[uuid.UUID(task_id)] if task_id else [],
            reward_item_ids=[item.reward_item_id],
            honor_ids_before=honors_before,
            whitelist_numbers=[student_number],
        )
