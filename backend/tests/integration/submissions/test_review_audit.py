# backend/tests/integration/submissions/test_review_audit.py
"""The durable audit rows of the three review decisions (spec §30; PR
#2 hardening pass 4a: G11/G12).

Each §30-listed surface lands exactly one ``audit_logs`` row in the
SAME transaction as the decision (the golden flush-only pattern):

- ``SUBMISSION_APPROVED`` on approve: before/after carry the
  submission/claim/lock state migration and the granted points; the
  idempotent ALREADY_REVIEWED replay writes NO second row;
- ``SUBMISSION_REVISION_REQUIRED`` on require_revision: the state
  migration plus the surviving lock status, the teacher note as the
  free-text reason;
- ``REWARD_LOCK_INVALIDATED`` on invalidate_reward_lock: the lock
  migration plus the REDACTED validation-report summary (counts and
  file facts only — the report's preview rows quote user content and
  must never reach a snapshot, G11);

and the HTTP surface threads ``AuditContext``: the propagated
``X-Request-ID`` and the directly connected peer ip land on the row.

PII negative assertion (G11): the seeded student's distinctive
nickname and phone appear in the validation report's preview content
— they must appear in NO audit snapshot of any of the three actions.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.db.session import get_db_session
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.identity.dependencies import (
    get_access_token_codec,
    get_business_clock,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor, InMemoryEventCollector
from app.modules.identity.models import TotpCredential, User
from app.modules.identity.session_service import SessionService
from app.modules.submissions.models import Submission
from app.modules.submissions.review_service import (
    AUDIT_ACTION_REWARD_LOCK_INVALIDATED,
    AUDIT_ACTION_SUBMISSION_APPROVED,
    AUDIT_ACTION_SUBMISSION_REVISION_REQUIRED,
    ReviewService,
)
from app.modules.submissions.router import get_event_publisher, get_points_port
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
from tests.fakes.points import FakePointsRewardPort

pytestmark = pytest.mark.integration

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)
# Anchored to the REAL now: the access-token guard validates exp
# against the wall clock (the existing API-test convention).
_T0 = datetime.now(UTC).replace(microsecond=0)
_GRACE = _T0 + timedelta(days=4)

# The seeded student's PII: distinctive so a leak cannot hide in noise,
# and deliberately ALSO present inside the validation report's preview
# content (the redaction rule must strip the report, not just avoid it).
_STUDENT_NICKNAME = "考研小助手帕曼"
_STUDENT_PHONE = "+8613800000001"
_STUDENT_NUMBER = "2025013901"


def _report_json() -> dict[str, Any]:
    """A §12.4 report whose preview quotes the student's PII — the
    invalidation summary must carry the counts, never this content."""
    return {
        "parser_version": "csv-1",
        "file_type": "CSV",
        "row_count": 3,
        "detected_columns": ["url", "title"],
        "missing_required_columns": [],
        "extra_columns": [],
        "type_error_counts": {},
        "null_ratios": {"url": 0.0, "title": 0.0},
        "duplicate_counts": {"url": 1},
        "warnings": ["重复 URL 行已合并"],
        "errors": [],
        "duration_ms": 12.5,
        "preview_rows": [[f"https://x/{_STUDENT_NUMBER}", _STUDENT_NICKNAME]],
    }


async def _reviewable_world(db: AsyncSession) -> dict[str, Any]:
    """Owner + student + a reviewable VALIDATED submission under a
    PROVISIONAL 100% lock (the same shape test_submission_api seeds)."""
    owner = User(
        username=f"revaud-t{uuid4().hex[:8]}",
        password_hash=_PASSWORD_HASH,
        nickname="审核教师阿德尔",
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
    )
    student = User(
        username=_STUDENT_NUMBER,
        password_hash=_PASSWORD_HASH,
        nickname=_STUDENT_NICKNAME,
        phone_e164=_STUDENT_PHONE,
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )
    db.add_all((owner, student))
    await db.flush()
    task = Task(
        owner_teacher_id=owner.id,
        title="小红书考研经验帖数据采集",
        description="采集指定关键词下的笔记正文与互动数据。",
        task_type=TaskType.DATA_CRAWL,
        rarity=TaskRarity.NORMAL,
        base_reward_points=100,
        status=TaskStatus.PUBLISHED,
        deadline_mode=DeadlineMode.RELATIVE,
        duration_minutes=4320,
        submission_schema={"columns": ["url", "title"]},
        submission_schema_version=1,
        allowed_file_types=["CSV"],
        max_file_size_bytes=10 * 1024 * 1024,
        notification_channels=["SMS"],
    )
    db.add(task)
    await db.flush()
    assignment = Assignment(
        task_id=task.id,
        platform="xiaohongshu",
        keyword=f"考研{uuid4().hex[:6]}",
        availability_status=AssignmentAvailability.OCCUPIED.value,
    )
    db.add(assignment)
    await db.flush()
    claim = AssignmentClaim(
        assignment_id=assignment.id,
        task_id=task.id,
        user_id=student.id,
        status=ClaimStatus.UNDER_REVIEW.value,
        claimed_at=_T0 - timedelta(days=1),
        deadline_at=_T0,
        grace_deadline_at=_GRACE,
        reward_policy_snapshot={"version": 1},
        base_reward_points_snapshot=100,
        submission_schema_version=1,
        reward_lock_status=RewardLockStatus.PROVISIONAL.value,
        reward_tier_locked=100,
        locked_reward_points=100,
        reward_locked_at=_T0 - timedelta(hours=2),
    )
    db.add(claim)
    await db.flush()
    submission = Submission(
        claim_id=claim.id,
        version=1,
        object_key=f"submissions/{claim.id}/{uuid4()}",
        original_filename="数据.csv",
        declared_type="CSV",
        detected_type="CSV",
        file_size=128,
        submitted_at=_T0 - timedelta(hours=2),
        validation_status="VALIDATED",
        review_status="PENDING_REVIEW",
        validation_report=_report_json(),
        retention_until=_T0 + timedelta(days=180),
    )
    db.add(submission)
    await db.flush()
    claim.latest_submission_id = submission.id
    await db.commit()  # close the seeding savepoint segment
    return {
        "owner": owner,
        "student": student,
        "task": task,
        "claim": claim,
        "submission": submission,
    }


def _service() -> tuple[ReviewService, InMemoryEventCollector]:
    collector = InMemoryEventCollector()
    return (
        ReviewService(
            clock=FrozenClock(_T0), events=collector, points=FakePointsRewardPort()
        ),
        collector,
    )


async def _audit_rows(db: AsyncSession, submission_id: Any) -> list[AuditLog]:
    return list(
        (
            await db.scalars(
                select(AuditLog)
                .where(AuditLog.target_id == str(submission_id))
                .order_by(AuditLog.created_at, AuditLog.id)
            )
        ).all()
    )


def _assert_no_pii(row: AuditLog) -> None:
    """G11 negative assertion: none of the row's JSON columns carries
    the seeded student's nickname, phone, or 学号."""
    for column in ("before_snapshot", "after_snapshot", "details"):
        blob = json.dumps(getattr(row, column) or {}, ensure_ascii=False)
        assert _STUDENT_NICKNAME not in blob, f"{column} leaked nickname"
        assert _STUDENT_PHONE not in blob, f"{column} leaked phone"
        assert _STUDENT_NUMBER not in blob, f"{column} leaked 学号"


# --- approve (§30 submission approval) ------------------------------------------------


async def test_approve_writes_one_audit_row_with_the_state_migration(
    db_session: AsyncSession,
) -> None:
    world = await _reviewable_world(db_session)
    submission = world["submission"]
    owner = world["owner"]
    service, _events = _service()

    result = await service.approve_submission(
        db_session, Actor(user_id=owner.id, role=Role.TEACHER), submission.id
    )
    assert result.already_reviewed is False

    rows = await _audit_rows(db_session, submission.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == AUDIT_ACTION_SUBMISSION_APPROVED
    assert row.target_type == "submission"
    assert row.target_id == str(submission.id)
    assert row.actor_user_id == owner.id
    assert row.actor_role == Role.TEACHER.value
    assert row.reason is None  # approve carries no free-text why
    assert row.before_snapshot == {
        "submission_review_status": "PENDING_REVIEW",
        "claim_status": "UNDER_REVIEW",
        "reward_lock_status": "PROVISIONAL",
        "reward_tier_locked": 100,
        "locked_reward_points": 100,
    }
    assert row.after_snapshot == {
        "submission_review_status": "APPROVED",
        "claim_status": "COMPLETED",
        "reward_lock_status": "CONFIRMED",
        "reward_tier_locked": 100,
        "locked_reward_points": 100,
        "points_granted": 100,
    }
    assert row.details == {
        "task_id": str(world["task"].id),
        "claim_id": str(world["claim"].id),
        "submission_id": str(submission.id),
    }
    # Service-level call: no request context, so the correlation pair
    # stays NULL (the documented non-HTTP default).
    assert row.ip_address is None
    assert row.request_id is None
    assert row.created_at is not None
    _assert_no_pii(row)


async def test_approve_idempotent_replay_writes_no_second_row(
    db_session: AsyncSession,
) -> None:
    """§14 幂等成功 + 金样幂等重放不写: the replay answers
    ALREADY_REVIEWED and the audit trail keeps exactly one row."""
    world = await _reviewable_world(db_session)
    submission = world["submission"]
    owner = world["owner"]
    service, _events = _service()
    actor = Actor(user_id=owner.id, role=Role.TEACHER)

    first = await service.approve_submission(db_session, actor, submission.id)
    replay = await service.approve_submission(db_session, actor, submission.id)
    assert first.already_reviewed is False
    assert replay.already_reviewed is True

    rows = await _audit_rows(db_session, submission.id)
    assert len(rows) == 1
    assert rows[0].action == AUDIT_ACTION_SUBMISSION_APPROVED


# --- require_revision (§30 revision/return) -------------------------------------------


async def test_require_revision_audits_the_migration_and_note(
    db_session: AsyncSession,
) -> None:
    world = await _reviewable_world(db_session)
    submission = world["submission"]
    owner = world["owner"]
    service, _events = _service()

    claim = await service.require_revision(
        db_session,
        Actor(user_id=owner.id, role=Role.TEACHER),
        submission.id,
        "缺少来源列，请补充。",
    )

    rows = await _audit_rows(db_session, submission.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == AUDIT_ACTION_SUBMISSION_REVISION_REQUIRED
    assert row.reason == "缺少来源列，请补充。"
    assert row.before_snapshot == {
        "submission_review_status": "PENDING_REVIEW",
        "claim_status": "UNDER_REVIEW",
        "reward_lock_status": "PROVISIONAL",
    }
    assert row.after_snapshot == {
        "submission_review_status": "REVISION_REQUIRED",
        "claim_status": "REVISION_REQUIRED",
        # §11.3: the existing lock survives the ordinary quality loop.
        "reward_lock_status": "PROVISIONAL",
        "revision_deadline_at": claim.revision_deadline_at.isoformat(),
    }
    assert row.ip_address is None
    assert row.request_id is None
    assert row.created_at is not None
    _assert_no_pii(row)


# --- invalidate_reward_lock (§30 malicious-lock invalidation) -------------------------


async def test_invalidate_audits_the_lock_migration_and_redacted_basis(
    db_session: AsyncSession,
) -> None:
    world = await _reviewable_world(db_session)
    submission = world["submission"]
    owner = world["owner"]
    service, _events = _service()

    claim = await service.invalidate_reward_lock(
        db_session,
        Actor(user_id=owner.id, role=Role.TEACHER),
        submission.id,
        "空壳提交：仅含表头，无任何数据行。",
    )

    rows = await _audit_rows(db_session, submission.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == AUDIT_ACTION_REWARD_LOCK_INVALIDATED
    assert row.reason == "空壳提交：仅含表头，无任何数据行。"
    assert row.before_snapshot == {
        "submission_review_status": "PENDING_REVIEW",
        "claim_status": "UNDER_REVIEW",
        "reward_lock_status": "PROVISIONAL",
        "reward_tier_locked": 100,
        "locked_reward_points": 100,
    }
    after = row.after_snapshot
    assert after is not None
    assert after["submission_review_status"] == "REVISION_REQUIRED"
    assert after["claim_status"] == "REVISION_REQUIRED"
    assert after["reward_lock_status"] == "INVALIDATED"
    assert after["reward_tier_locked"] is None
    assert after["locked_reward_points"] is None
    assert after["revision_deadline_at"] == claim.revision_deadline_at.isoformat()
    # The 依据: counts and file facts only — preview content redacted.
    assert after["validation_report_summary"] == {
        "present": True,
        "file_type": "CSV",
        "row_count": 3,
        "error_count": 0,
        "warning_count": 1,
    }
    assert row.ip_address is None
    assert row.request_id is None
    _assert_no_pii(row)


# --- the HTTP seam: AuditContext lands on the row -------------------------------------


@pytest.fixture
def api_clock() -> FrozenClock:
    return FrozenClock(_T0)


@pytest.fixture
def event_collector() -> InMemoryEventCollector:
    return InMemoryEventCollector()


@pytest.fixture
def fake_points() -> FakePointsRewardPort:
    return FakePointsRewardPort()


@pytest_asyncio.fixture
async def api_app(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    event_collector: InMemoryEventCollector,
    fake_points: FakePointsRewardPort,
) -> FastAPI:
    app = create_app()

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _test_db_session
    app.dependency_overrides[get_business_clock] = lambda: api_clock
    app.dependency_overrides[get_event_publisher] = lambda: event_collector
    app.dependency_overrides[get_points_port] = lambda: fake_points
    return app


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


async def _owner_headers(
    db: AsyncSession, clock: FrozenClock, owner: User
) -> dict[str, str]:
    db.add(
        TotpCredential(
            user_id=owner.id,
            secret_encrypted=b"test-stand-in-secret",
            confirmed_at=clock.now(),
        )
    )
    sessions = SessionService(clock=clock, access_codec=get_access_token_codec())
    _, tokens = await sessions.issue_session(db, user=owner, now=clock.now())
    return {"Authorization": f"Bearer {tokens.access_token}"}


async def test_http_review_actions_carry_request_id_and_ip(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    """The router constructs AuditContext.from_request for the review
    endpoints: the propagated X-Request-ID and the directly connected
    peer ip land on the durable row (spec §30)."""
    world = await _reviewable_world(db_session)
    submission = world["submission"]
    headers = await _owner_headers(db_session, api_clock, world["owner"])
    headers["X-Request-ID"] = "revaud-approve-0001"

    response = await client.post(
        f"/api/v1/teacher/submissions/{submission.id}/approve",
        headers=headers,
    )
    assert response.status_code == 200, response.text

    rows = await _audit_rows(db_session, submission.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == AUDIT_ACTION_SUBMISSION_APPROVED
    assert row.request_id == "revaud-approve-0001"
    # httpx ASGITransport reports the local peer 127.0.0.1.
    assert row.ip_address == "127.0.0.1"
    _assert_no_pii(row)


async def test_http_without_request_id_header_still_correlates(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    """No client header still correlates: the audit row carries the
    middleware-generated request id — the SAME id the response's
    X-Request-ID returns (round-5 P1: the server's identifier for the
    actual request, not a fabrication of one the client never made)."""
    world = await _reviewable_world(db_session)
    submission = world["submission"]
    headers = await _owner_headers(db_session, api_clock, world["owner"])

    response = await client.post(
        f"/api/v1/teacher/submissions/{submission.id}/revision-required",
        json={"note": "请补充来源列。"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    resolved_request_id = response.headers["X-Request-ID"]

    rows = await _audit_rows(db_session, submission.id)
    assert len(rows) == 1
    assert rows[0].action == AUDIT_ACTION_SUBMISSION_REVISION_REQUIRED
    assert rows[0].request_id == resolved_request_id
    assert rows[0].ip_address == "127.0.0.1"


async def test_http_invalidate_carries_request_id_and_ip(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    """The third review endpoint threads the same AuditContext seam."""
    world = await _reviewable_world(db_session)
    submission = world["submission"]
    headers = await _owner_headers(db_session, api_clock, world["owner"])
    headers["X-Request-ID"] = "revaud-invalidate-0002"

    response = await client.post(
        f"/api/v1/teacher/submissions/{submission.id}/invalidate-reward-lock",
        json={"reason": "空壳提交：仅含表头。"},
        headers=headers,
    )
    assert response.status_code == 200, response.text

    rows = await _audit_rows(db_session, submission.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == AUDIT_ACTION_REWARD_LOCK_INVALIDATED
    assert row.request_id == "revaud-invalidate-0002"
    assert row.ip_address == "127.0.0.1"
    _assert_no_pii(row)
