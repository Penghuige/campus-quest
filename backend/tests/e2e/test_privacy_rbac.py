# backend/tests/e2e/test_privacy_rbac.py
"""Privacy and authorization gate (plan 10 task 9; G10/G11/G12).

Four release-gate invariants, every one through the real API with real
minted tokens (the W6 single-domain matrix widened to the e2e
integration shape):

1. **Serialized negative search (G11):** a seeded student's student
   number, phone, email, and upload ``object_key`` are full-text
   searched against every response another student and the staff
   surfaces can reach — task lists/details, comments, boards,
   notifications, the review queue, the moderation view. What no DTO
   carries cannot leak, and this gate proves no DTO carries it.
2. **Anonymous comment privacy:** the public list renders 匿名用户; the
   Teacher governance (moderation) view shows the pseudonymous
   moderation key instead of identity — and the ADMIN normal list
   stays just as anonymous (de-anonymization is not a listing
   property).
3. **Explicit reveal (G12):** the Admin reveal succeeds with a reason
   and writes the durable ``COMMUNITY_IDENTITY_REVEAL`` AuditLog row
   (actor/target/reason); a missing reason is the typed rejection.
4. **Direct-route RBAC matrix:** Student/Teacher/Admin call every
   sensitive route class — the admin surface, the teacher management
   surface, student-only writes, and the community participant surface
   — and each (role, route) cell answers its exact expected status.
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
from app.modules.identity.models import User
from app.modules.submissions.enums import FileType
from app.modules.submissions.models import UploadIntent
from app.modules.tasks.models import AssignmentClaim
from tests.e2e.factories import (
    CSV_SUBMISSION_SCHEMA,
    clean_world,
    seed_admin_confirmed_totp,
    seed_student,
    seed_task_with_assignments,
    seed_teacher_confirmed_totp,
    snapshot_honor_ids,
)
from tests.e2e.test_happy_path import (
    _mint_access_token,
    _purge_objects,
    _put,
)

pytestmark = pytest.mark.e2e

_BROKER_QUEUE_KEY = "celery"

_GOOD_CSV = (
    b"url,title\n"
    b"https://example.com/note/1,\xe7\xac\xac\xe4\xb8\x80\xe6\x9d\xa1\n"
    b"https://example.com/note/2,\xe7\xac\xac\xe4\xba\x8c\xe6\x9d\xa1\n"
)


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


async def _set_email(
    db_factory: async_sessionmaker[AsyncSession], user_id: UUID, email: str
) -> None:
    async with db_factory() as db:
        user = await db.get(User, user_id)
        assert user is not None
        user.email_normalized = email
        await db.commit()


async def _object_key_of(
    db_factory: async_sessionmaker[AsyncSession], claim_id: UUID
) -> str:
    async with db_factory() as db:
        key = await db.scalar(
            select(UploadIntent.object_key).where(UploadIntent.claim_id == claim_id)
        )
        assert key is not None
        return key


async def _bearer_headers(
    db_factory: async_sessionmaker[AsyncSession], user_id: UUID
) -> dict[str, str]:
    """Real minted-token bearer headers for one seeded user."""
    return {"Authorization": f"Bearer {await _mint_access_token(db_factory, user_id)}"}


def _assert_no_secrets(body: str, secrets: dict[str, str], where: str) -> None:
    """Full-text negative search over one serialized response."""
    for name, value in secrets.items():
        assert value not in body, (where, name)


async def test_public_and_student_surfaces_exclude_sensitive_fields(
    db_factory: async_sessionmaker[AsyncSession], _stack: dict[str, Any]
) -> None:
    """Negative search: the seeded student's number/phone/email/object
    key appear in NO student-reachable or staff response (G11 — the DTO
    shapes enforce it; this gate proves the shapes hold end to end)."""
    run = uuid.uuid4().hex[:12]
    honors_before = await snapshot_honor_ids(db_factory)
    teacher = await seed_teacher_confirmed_totp(db_factory, run=f"t{run}")
    admin = await seed_admin_confirmed_totp(db_factory, run=f"a{run}")
    holder = await seed_student(db_factory, run=f"1{run}")
    viewer = await seed_student(db_factory, run=f"2{run}")
    task = await seed_task_with_assignments(
        db_factory, teacher_id=teacher.user_id, run=run, assignment_count=2
    )

    holder_email = f"e2e-privacy-{run}@school.edu"
    await _set_email(db_factory, holder.user_id, holder_email)
    # World-visible: the holder's phone is the factories' deterministic
    # value — read it straight from the row the same way a leak would.
    async with db_factory() as db:
        holder_row = await db.get(User, holder.user_id)
        assert holder_row is not None
        holder_phone = holder_row.phone_e164
    assert holder_phone is not None

    secrets = {
        "student_number": holder.username,
        "phone": holder_phone,
        "email": holder_email,
    }

    holder_headers = await _bearer_headers(db_factory, holder.user_id)
    viewer_headers = await _bearer_headers(db_factory, viewer.user_id)
    teacher_headers = await _bearer_headers(db_factory, teacher.user_id)
    try:
        # The holder really claims and uploads, so an object_key exists
        # to leak (the presign response is the OWNER's — excluded from
        # the negative search by design).
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_stack["app"]),
            base_url="http://e2e-privacy",
        ) as client:
            claimed = await client.post(
                f"/api/v1/tasks/{task.task_id}/claim", headers=holder_headers
            )
            assert claimed.status_code == 201, claimed.text
            claim_id = uuid.UUID(claimed.json()["claim_id"])
            intent_response = await client.post(
                "/api/v1/submissions/upload-intent",
                headers=holder_headers,
                json={
                    "claim_id": str(claim_id),
                    "filename": "隐私负搜索.csv",
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
                headers=holder_headers,
                json={"intent_id": intent["intent_id"]},
            )
            assert completed.status_code == 200, completed.text
            submission_id = completed.json()["id"]
        secrets["object_key"] = await _object_key_of(db_factory, claim_id)

        # The holder posts one ANONYMOUS comment (the §21.4 surface).
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_stack["app"]),
            base_url="http://e2e-privacy",
        ) as client:
            posted = await client.post(
                f"/api/v1/tasks/{task.task_id}/comments",
                headers=holder_headers,
                json={"content": f"匿名负搜索{run[:6]}", "is_anonymous": True},
            )
            assert posted.status_code == 201, posted.text

        # Every viewer-reachable surface, serialized and searched.
        surfaces: list[tuple[str, str, dict[str, str]]] = [
            ("GET", "/api/v1/tasks", viewer_headers),
            ("GET", f"/api/v1/tasks/{task.task_id}", viewer_headers),
            ("GET", f"/api/v1/tasks/{task.task_id}/comments", viewer_headers),
            ("GET", "/api/v1/rankings/daily", viewer_headers),
            ("GET", "/api/v1/rankings/monthly", viewer_headers),
            ("GET", "/api/v1/rankings/all", viewer_headers),
            ("GET", "/api/v1/rankings/around-me", viewer_headers),
            ("GET", "/api/v1/rewards", viewer_headers),
            ("GET", "/api/v1/notifications", viewer_headers),
            ("GET", "/api/v1/me/claims", viewer_headers),
            ("GET", "/api/v1/points/me", viewer_headers),
        ]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_stack["app"]),
            base_url="http://e2e-privacy",
        ) as client:
            for method, path, headers in surfaces:
                response = await client.request(method, path, headers=headers)
                assert response.status_code == 200, (path, response.status_code)
                _assert_no_secrets(response.text, secrets, path)

            # Staff surfaces over the SAME world: the review queue sees
            # the holder's submission row, the moderation view sees the
            # anonymous comment — neither may carry the secrets.
            staff_surfaces = [
                ("GET", "/api/v1/teacher/submissions/review-queue", teacher_headers),
                (
                    "GET",
                    f"/api/v1/teacher/tasks/{task.task_id}/comments/moderation",
                    teacher_headers,
                ),
                (
                    "GET",
                    f"/api/v1/tasks/{task.task_id}/comments",
                    await _bearer_headers(db_factory, admin.user_id),
                ),
                (
                    "GET",
                    f"/api/v1/teacher/submissions/{submission_id}/validation",
                    viewer_headers,  # 404/403 for a non-owner: still no secrets
                ),
            ]
            for method, path, headers in staff_surfaces:
                response = await client.request(method, path, headers=headers)
                assert response.status_code < 500, (path, response.status_code)
                _assert_no_secrets(response.text, secrets, path)
    finally:
        await _purge_objects(db_factory, [task.task_id])
        async with db_factory() as db:
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
                holder.user_id,
                viewer.user_id,
            ],
            task_ids=[task.task_id],
            honor_ids_before=honors_before,
        )


async def test_anonymous_privacy_and_explicit_reveal(
    db_factory: async_sessionmaker[AsyncSession], _stack: dict[str, Any]
) -> None:
    """Anonymous comments stay pseudonymous on the student, Teacher
    governance, and Admin normal views; the reveal succeeds with a
    reason (one durable AuditLog row) and is refused without one."""
    run = uuid.uuid4().hex[:12]
    honors_before = await snapshot_honor_ids(db_factory)
    teacher = await seed_teacher_confirmed_totp(db_factory, run=f"t{run}")
    admin = await seed_admin_confirmed_totp(db_factory, run=f"a{run}")
    author = await seed_student(db_factory, run=f"1{run}")
    viewer = await seed_student(db_factory, run=f"2{run}")
    async with db_factory() as db:
        author_row = await db.get(User, author.user_id)
        assert author_row is not None
        author_nickname = author_row.nickname
    task = await seed_with_claimed(
        db_factory, teacher_id=teacher.user_id, run=run, student_id=author.user_id
    )

    author_headers = await _bearer_headers(db_factory, author.user_id)
    viewer_headers = await _bearer_headers(db_factory, viewer.user_id)
    teacher_headers = await _bearer_headers(db_factory, teacher.user_id)
    admin_headers = await _bearer_headers(db_factory, admin.user_id)
    try:
        content = f"匿名治理{run[:6]}"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_stack["app"]),
            base_url="http://e2e-privacy",
        ) as client:
            posted = await client.post(
                f"/api/v1/tasks/{task}/comments",
                headers=author_headers,
                json={"content": content, "is_anonymous": True},
            )
            assert posted.status_code == 201, posted.text
            comment_id = posted.json()["id"]

            # Student surface: uniform 匿名用户, no identity hooks.
            public_list = await client.get(
                f"/api/v1/tasks/{task}/comments", headers=viewer_headers
            )
            assert public_list.status_code == 200, public_list.text
            public_body = public_list.json()
            entry = next(
                item for item in public_body["items"] if item["id"] == comment_id
            )
            assert entry["author_display"] == "匿名用户"
            assert author.username not in public_list.text

            # Teacher governance view: pseudonymous key, still no identity.
            moderation = await client.get(
                f"/api/v1/teacher/tasks/{task}/comments/moderation",
                headers=teacher_headers,
            )
            assert moderation.status_code == 200, moderation.text
            moderated = next(
                item for item in moderation.json()["items"] if item["id"] == comment_id
            )
            assert moderated["author_display"] == "匿名用户"
            assert moderated["moderation_key"] is not None
            assert author.username not in moderation.text
            assert author_nickname not in moderation.text

            # Admin normal list: exactly as anonymous as the teacher's.
            admin_list = await client.get(
                f"/api/v1/teacher/tasks/{task}/comments/moderation",
                headers=admin_headers,
            )
            assert admin_list.status_code == 200, admin_list.text
            admin_entry = next(
                item for item in admin_list.json()["items"] if item["id"] == comment_id
            )
            assert admin_entry["author_display"] == "匿名用户"
            assert author.username not in admin_list.text

            # Missing reason: the typed rejection (blank body -> 422 at
            # the schema; whitespace-only -> the service's 400).
            blank = await client.post(
                f"/api/v1/admin/comments/{comment_id}/reveal-identity",
                headers=admin_headers,
                json={"reason": ""},
            )
            assert blank.status_code == 422, blank.text
            whitespace = await client.post(
                f"/api/v1/admin/comments/{comment_id}/reveal-identity",
                headers=admin_headers,
                json={"reason": "   "},
            )
            assert whitespace.status_code == 400, whitespace.text
            assert whitespace.json()["error"]["code"] == "VALIDATION_ERROR"

            # The audited reveal: identity ONLY here, plus the durable
            # AuditLog row (actor/target/reason).
            reason = f"e2e 隐私 gate 追溯 {run}"
            revealed = await client.post(
                f"/api/v1/admin/comments/{comment_id}/reveal-identity",
                headers=admin_headers,
                json={"reason": reason},
            )
            assert revealed.status_code == 200, revealed.text
            identity = revealed.json()
            assert identity["user_id"] == str(author.user_id)
            assert identity["username"] == author.username
            assert identity["nickname"]

        async with db_factory() as db:
            audit_row = await db.scalar(
                select(AuditLog).where(
                    AuditLog.actor_user_id == admin.user_id,
                    AuditLog.action == "COMMUNITY_IDENTITY_REVEAL",
                    AuditLog.target_id == str(comment_id),
                )
            )
            assert audit_row is not None
            assert audit_row.reason == reason
            assert audit_row.target_type == "comment"
            assert audit_row.actor_role == "ADMIN"

            # The listing AFTER the reveal is still anonymous (a reveal
            # is an access event, never a state change).
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_stack["app"]),
            base_url="http://e2e-privacy",
        ) as client:
            still_anonymous = await client.get(
                f"/api/v1/tasks/{task}/comments", headers=viewer_headers
            )
            entry_after = next(
                item
                for item in still_anonymous.json()["items"]
                if item["id"] == comment_id
            )
            assert entry_after["author_display"] == "匿名用户"
    finally:
        await _purge_objects(db_factory, [task])
        async with db_factory() as db:
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
                author.user_id,
                viewer.user_id,
            ],
            task_ids=[task],
            honor_ids_before=honors_before,
        )


async def seed_with_claimed(
    db_factory: async_sessionmaker[AsyncSession],
    *,
    teacher_id: UUID,
    run: str,
    student_id: UUID,
) -> UUID:
    """One published task whose first assignment the student has an
    ORM-claimed (COMPLETED) claim on — the rating/eligibility surfaces
    need a completer; the claim's shape mirrors ``factories.seed_claim``
    with a terminal status."""
    from app.modules.tasks.claim_service import REWARD_POLICY_SNAPSHOT_V1
    from app.modules.tasks.deadlines import compute_claim_deadlines
    from app.modules.tasks.enums import (
        AssignmentAvailability,
        ClaimStatus,
        DeadlineMode,
        RewardLockStatus,
        TaskRarity,
        TaskStatus,
        TaskType,
    )
    from app.modules.tasks.models import Assignment, Task

    async with db_factory() as db:
        now = datetime.now(UTC)
        task = Task(
            owner_teacher_id=teacher_id,
            title=f"隐私gate任务{run[:6]}",
            description="隐私/RBAC gate 的目标任务。",
            task_type=TaskType.DATA_CRAWL,
            rarity=TaskRarity.NORMAL,
            base_reward_points=100,
            status=TaskStatus.PUBLISHED,
            deadline_mode=DeadlineMode.RELATIVE,
            duration_minutes=4320,
            submission_schema=dict(CSV_SUBMISSION_SCHEMA),
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
            keyword=f"隐私{run[:6]}",
            availability_status=AssignmentAvailability.OCCUPIED,
        )
        db.add(assignment)
        await db.flush()
        deadlines = compute_claim_deadlines(task, now)
        db.add(
            AssignmentClaim(
                assignment_id=assignment.id,
                task_id=task.id,
                user_id=student_id,
                status=ClaimStatus.COMPLETED,
                claimed_at=now,
                deadline_at=deadlines.deadline_at,
                grace_deadline_at=deadlines.grace_deadline_at,
                reward_policy_snapshot=dict(REWARD_POLICY_SNAPSHOT_V1),
                base_reward_points_snapshot=task.base_reward_points,
                submission_schema_version=task.submission_schema_version,
                reward_lock_status=RewardLockStatus.CONFIRMED,
                terminal_at=now,
            )
        )
        await db.commit()
        return task.id


async def test_direct_route_rbac_matrix(
    db_factory: async_sessionmaker[AsyncSession], _stack: dict[str, Any]
) -> None:
    """Student/Teacher/Admin x the sensitive route classes, every cell
    an exact expected status through the real token chain."""
    run = uuid.uuid4().hex[:12]
    honors_before = await snapshot_honor_ids(db_factory)
    teacher = await seed_teacher_confirmed_totp(db_factory, run=f"t{run}")
    admin = await seed_admin_confirmed_totp(db_factory, run=f"a{run}")
    student = await seed_student(db_factory, run=f"1{run}")
    author = await seed_student(db_factory, run=f"2{run}")
    task = await seed_task_with_assignments(
        db_factory, teacher_id=teacher.user_id, run=run, assignment_count=3
    )

    tokens = {
        "student": await _mint_access_token(db_factory, student.user_id),
        "teacher": await _mint_access_token(db_factory, teacher.user_id),
        "admin": await _mint_access_token(db_factory, admin.user_id),
    }
    created_task_ids: list[UUID] = []
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_stack["app"]),
            base_url="http://e2e-privacy",
        ) as client:
            # An anonymous comment as the reveal matrix's target.
            author_headers = await _bearer_headers(db_factory, author.user_id)
            posted = await client.post(
                f"/api/v1/tasks/{task.task_id}/comments",
                headers=author_headers,
                json={"content": f"矩阵目标{run[:6]}", "is_anonymous": True},
            )
            assert posted.status_code == 201, posted.text
            comment_id = posted.json()["id"]

            task_create_body = {
                "title": f"矩阵任务{run[:6]}",
                "description": "RBAC 矩阵创建的任务。",
                "base_reward_points": 100,
                "deadline_mode": "RELATIVE",
                "allowed_file_types": ["CSV"],
                "max_file_size_bytes": 10 * 1024 * 1024,
                "duration_minutes": 4320,
                "submission_schema": CSV_SUBMISSION_SCHEMA,
                "submission_schema_version": 1,
                "notification_channels": ["SMS"],
            }
            comment_body = {"content": f"矩阵评论{run[:6]}", "is_anonymous": False}

            # (method, path, body, {role: expected status}) — the exact
            # matrix the guards define (identity/dependencies.py):
            # admin-only surfaces, the staff management family, the
            # student-only claim lifecycle, and the participant family
            # (Student + Teacher, Admin excluded by the hardening
            # ruling).
            matrix: list[tuple[str, str, Any, dict[str, int]]] = [
                (
                    "GET",
                    "/api/v1/admin/whitelist",
                    None,
                    {"admin": 200, "teacher": 403, "student": 403},
                ),
                (
                    "POST",
                    "/api/v1/teacher/tasks",
                    task_create_body,
                    {"teacher": 201, "admin": 201, "student": 403},
                ),
                (
                    "POST",
                    f"/api/v1/tasks/{task.task_id}/claim",
                    None,
                    {"student": 201, "teacher": 403, "admin": 403},
                ),
                (
                    "POST",
                    f"/api/v1/tasks/{task.task_id}/comments",
                    comment_body,
                    {"student": 201, "teacher": 201, "admin": 403},
                ),
                (
                    "POST",
                    f"/api/v1/admin/comments/{comment_id}/reveal-identity",
                    {"reason": f"e2e 矩阵追溯 {run}"},
                    {"admin": 200, "teacher": 403, "student": 403},
                ),
            ]
            for method, path, body, expected in matrix:
                for role, status in expected.items():
                    response = await client.request(
                        method,
                        path,
                        headers={"Authorization": f"Bearer {tokens[role]}"},
                        json=body,
                    )
                    assert response.status_code == status, (
                        method,
                        path,
                        role,
                        response.status_code,
                        response.text,
                    )
                    if (
                        method == "POST"
                        and path == "/api/v1/teacher/tasks"
                        and status == 201
                    ):
                        # The matrix's own creates ride along into
                        # teardown (their owner users are deleted too).
                        created_task_ids.append(uuid.UUID(response.json()["id"]))
    finally:
        async with db_factory() as db:
            await db.execute(
                delete(AuditLog).where(
                    AuditLog.actor_user_id.in_([teacher.user_id, admin.user_id])
                )
            )
            await db.commit()
        await clean_world(
            db_factory,
            user_ids=[teacher.user_id, admin.user_id, student.user_id, author.user_id],
            task_ids=[task.task_id, *created_task_ids],
            honor_ids_before=honors_before,
        )
