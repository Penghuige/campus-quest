# backend/tests/integration/notifications/test_notification_api.py
"""The notification HTTP API end-to-end over real PostgreSQL (plan 07
T8; spec §25.4/§28): the student inbox and the staff failure query,
driven through the real app (``create_app()`` — real routes, real
envelope handlers, real composition-root wiring incl. the notifications
router mounted under /api/v1).

Pinned per the plan's steps:

- ownership: the inbox lists ONLY the caller's own Notification rows
  (another student's rows are absent from items and total); marking
  another user's notification answers PERMISSION_DENIED (403) and
  leaves that row unread; an unknown id answers NOT_FOUND (404).
- visibility (spec §25.2 on the IN_APP channel): only rows whose IN_APP
  delivery completed a real send (SENT without a "skipped:" marker)
  list; future-scheduled PENDING reminders, policy-skipped rows, and
  rows without an IN_APP delivery are absent from items and total —
  the composed dispatch-boundary proof (FrozenClock, exact
  scheduled_at) lives in test_inbox_visibility.py.
- student-only surface: a TEACHER token is rejected by the student
  guard (403), an anonymous call by the bearer gate (401).
- the DTO contract: title/body/read_at/created_at/event_type and the
  pagination envelope — no provider internals, no delivery-channel
  state, no other users' data.
- unread filter + offset pagination (the documented V1 choices), and
  the page-limit bound (422 above 50).
- mark-read idempotency: a re-read returns the FIRST read_at unchanged
  (pinned with a clock the test advances between the two calls).
- the staff failure surface (spec §25.4 "后台可查询失败原因"):
  FAILED deliveries with last_error + attempts, newest first, behind
  the Admin guard (PR #2 hardening ruling: Admin-only until scoped
  delegation — a student, a confirmed TOTP teacher, and a
  TOTP-unconfirmed admin are all 403).

Seams are dependency overrides, not route fakes: the rollback-harness
session and a mutable step clock (business time), the same pattern as
tests/integration/tasks/test_task_api.py.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.session import get_db_session
from app.main import create_app
from app.modules.identity.dependencies import (
    get_access_token_codec,
    get_business_clock,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import TotpCredential, User
from app.modules.identity.session_service import SessionService
from app.modules.notifications.enums import (
    SKIPPED_LAST_ERROR_PREFIX,
    DeliveryStatus,
    NotificationChannel,
    NotificationEventType,
)
from app.modules.notifications.models import Notification, NotificationDelivery

pytestmark = pytest.mark.integration

# Anchored to the real now: PyJWT validates `exp` at decode time
# against wall-clock time, so tokens minted by the app must be "just
# now" (the identity and tasks API suites use the same anchoring).
_T0 = datetime.now(UTC).replace(microsecond=0)
_PASSWORD = "correct-horse-battery"

_ITEM_FIELDS = {"id", "event_type", "title", "body", "read_at", "created_at"}


class StepClock:
    """Mutable business clock: the test owns "now" so idempotency is
    distinguishable from a frozen instant (backend-engineering §11)."""

    def __init__(self, start: datetime) -> None:
        self.current = start.astimezone(UTC)

    def advance(self, delta: timedelta) -> None:
        self.current += delta

    def now(self) -> datetime:
        return self.current


# --- fixtures and seeding helpers ----------------------------------------------------


@pytest.fixture
def api_clock() -> StepClock:
    return StepClock(_T0)


@pytest.fixture
def api_app(db_session: AsyncSession, api_clock: StepClock) -> FastAPI:
    app = create_app()

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _test_db_session
    app.dependency_overrides[get_business_clock] = lambda: api_clock
    return app


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


async def _seed_user(
    db: AsyncSession, *, username: str, role: Role = Role.STUDENT
) -> User:
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


async def _session_tokens(
    db: AsyncSession, clock: StepClock, user: User
) -> dict[str, str]:
    sessions = SessionService(clock=clock, access_codec=get_access_token_codec())
    _, tokens = await sessions.issue_session(db, user=user, now=clock.now())
    return {"Authorization": f"Bearer {tokens.access_token}"}


async def _seed_management_account(
    db: AsyncSession,
    clock: StepClock,
    *,
    username: str,
    role: Role = Role.ADMIN,
    confirmed: bool = True,
) -> tuple[User, dict[str, str]]:
    """A staff account meant to pass (or, with confirmed=False, fail)
    the guards: role + ACTIVE + a CONFIRMED credential row — the staff
    management guard for TEACHER/ADMIN, and (role=ADMIN, the failure
    surface's Admin-only ruling) the admin guard. The bytes are opaque
    to the guards (only confirmed_at is read)."""
    user = await _seed_user(db, username=username, role=role)
    if confirmed:
        db.add(
            TotpCredential(
                user_id=user.id,
                secret_encrypted=b"test-stand-in-bytes",
                confirmed_at=clock.now(),
            )
        )
        await db.flush()
    return user, await _session_tokens(db, clock, user)


def _seed_notification(
    user: User,
    *,
    event_key: str,
    created_at: datetime,
    read_at: datetime | None = None,
) -> Notification:
    # created_at/read_at are explicit: the inbox contract is ordering
    # and read state, and server-default now() would blur both.
    return Notification(
        user_id=user.id,
        event_key=event_key,
        event_type=NotificationEventType.SUBMISSION_APPROVED.value,
        title="任务审核通过",
        body="您的提交已通过审核。",
        read_at=read_at,
        created_at=created_at,
    )


def _seed_delivery(
    notification: Notification,
    *,
    status: DeliveryStatus,
    attempts: int = 0,
    updated_at: datetime | None = None,
    last_error: str | None = None,
) -> NotificationDelivery:
    delivery = NotificationDelivery(
        notification_id=notification.id,
        user_id=notification.user_id,
        event_key=notification.event_key,
        channel=NotificationChannel.IN_APP.value,
        status=status.value,
        scheduled_at=_T0 - timedelta(minutes=30),
        attempts=attempts,
        last_error=last_error,
    )
    if updated_at is not None:
        delivery.updated_at = updated_at
    return delivery


def _delivered(notification: Notification) -> NotificationDelivery:
    """An IN_APP delivery that completed a real send — the state that
    makes a Notification row an inbox message (spec §25.2 visibility
    gate, inbox_service): terminal SENT, no policy-skip marker."""
    return _seed_delivery(notification, status=DeliveryStatus.SENT, attempts=1)


def _envelope(response: httpx.Response) -> dict:
    body = response.json()
    assert set(body) == {"error"}, body
    error = body["error"]
    assert set(error) == {"code", "message", "details", "request_id"}, error
    assert error["request_id"] == response.headers["X-Request-ID"]
    return error


# --- ownership + listing (plan steps 3-4) ---------------------------------------------


async def test_inbox_lists_own_rows_newest_first(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: StepClock
) -> None:
    student = await _seed_user(db_session, username=f"stu-{uuid4().hex[:10]}")
    other = await _seed_user(db_session, username=f"stu-{uuid4().hex[:10]}")
    oldest = _seed_notification(
        student,
        event_key=f"submission:{uuid4()}:approved",
        created_at=_T0 - timedelta(hours=3),
    )
    middle = _seed_notification(
        student,
        event_key=f"submission:{uuid4()}:approved",
        created_at=_T0 - timedelta(hours=2),
    )
    newest = _seed_notification(
        student,
        event_key=f"submission:{uuid4()}:approved",
        created_at=_T0 - timedelta(hours=1),
    )
    foreign = _seed_notification(
        other,
        event_key=f"submission:{uuid4()}:approved",
        created_at=_T0 - timedelta(minutes=5),
    )
    db_session.add_all([oldest, middle, newest, foreign])
    await db_session.flush()  # server-default ids before the deliveries
    # The foreign row is delivery-complete too: its absence is then
    # purely the ownership scope, not the visibility gate.
    db_session.add_all(
        [
            _delivered(oldest),
            _delivered(middle),
            _delivered(newest),
            _delivered(foreign),
        ]
    )
    await db_session.flush()
    response = await client.get(
        "/api/v1/notifications",
        headers=await _session_tokens(db_session, api_clock, student),
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == 3
    assert body["limit"] == 20
    assert body["offset"] == 0
    assert [item["id"] for item in body["items"]] == [
        str(newest.id),
        str(middle.id),
        str(oldest.id),
    ]
    for item in body["items"]:
        assert set(item) == _ITEM_FIELDS, item
        assert item["event_type"] == "SUBMISSION_APPROVED"
        assert item["read_at"] is None
    # The other student's row is invisible: not in items, not in total.
    assert str(foreign.id) not in {item["id"] for item in body["items"]}


async def test_unread_filter_and_pagination(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: StepClock
) -> None:
    student = await _seed_user(db_session, username=f"stu-{uuid4().hex[:10]}")
    read_one = _seed_notification(
        student,
        event_key=f"submission:{uuid4()}:approved",
        created_at=_T0 - timedelta(hours=4),
        read_at=_T0 - timedelta(hours=3),
    )
    unread_old = _seed_notification(
        student,
        event_key=f"submission:{uuid4()}:approved",
        created_at=_T0 - timedelta(hours=3),
    )
    unread_new = _seed_notification(
        student,
        event_key=f"submission:{uuid4()}:approved",
        created_at=_T0 - timedelta(hours=2),
    )
    db_session.add_all([read_one, unread_old, unread_new])
    await db_session.flush()  # server-default ids before the deliveries
    db_session.add_all(
        [_delivered(read_one), _delivered(unread_old), _delivered(unread_new)]
    )
    await db_session.flush()
    headers = await _session_tokens(db_session, api_clock, student)

    unread = await client.get(
        "/api/v1/notifications", params={"unread": "true"}, headers=headers
    )
    assert unread.status_code == 200
    assert unread.json()["total"] == 2
    assert [item["id"] for item in unread.json()["items"]] == [
        str(unread_new.id),
        str(unread_old.id),
    ]

    page_one = await client.get(
        "/api/v1/notifications", params={"limit": 2, "offset": 0}, headers=headers
    )
    assert page_one.status_code == 200
    assert page_one.json()["total"] == 3
    assert [item["id"] for item in page_one.json()["items"]] == [
        str(unread_new.id),
        str(unread_old.id),
    ]
    page_two = await client.get(
        "/api/v1/notifications", params={"limit": 2, "offset": 2}, headers=headers
    )
    assert page_two.status_code == 200
    assert [item["id"] for item in page_two.json()["items"]] == [str(read_one.id)]

    # The documented bound: above 50 per page is a request error.
    rejected = await client.get(
        "/api/v1/notifications", params={"limit": 51}, headers=headers
    )
    assert rejected.status_code == 422


async def test_inbox_hides_undelivered_skipped_and_inapp_less_rows(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: StepClock
) -> None:
    """Spec §25.2 timing and cancellation for the IN_APP channel: the
    inbox lists only rows whose IN_APP delivery completed a real send.
    A future-scheduled PENDING reminder (the Notification row commits
    with the claim, hours before its scheduled_at), a policy-skipped
    row (SENT with the "skipped:" marker — e.g. the claim entered
    UNDER_REVIEW and dispatch cancelled the ordinary reminder), and a
    row with no IN_APP delivery at all are absent from items AND total;
    only the genuinely SENT one lists."""
    student = await _seed_user(db_session, username=f"stu-{uuid4().hex[:10]}")
    pending = _seed_notification(
        student,
        event_key=f"claim:{uuid4()}:deadline_4h",
        created_at=_T0 - timedelta(hours=1),
    )
    skipped = _seed_notification(
        student,
        event_key=f"claim:{uuid4()}:deadline_24h",
        created_at=_T0 - timedelta(hours=1),
    )
    sms_only = _seed_notification(
        student,
        event_key=f"submission:{uuid4()}:approved",
        created_at=_T0 - timedelta(hours=1),
    )
    delivered = _seed_notification(
        student,
        event_key=f"submission:{uuid4()}:approved",
        created_at=_T0 - timedelta(hours=2),
    )
    db_session.add_all([pending, skipped, sms_only, delivered])
    await db_session.flush()  # server-default ids before the deliveries
    pending_delivery = _seed_delivery(pending, status=DeliveryStatus.PENDING)
    pending_delivery.scheduled_at = _T0 + timedelta(hours=4)
    skipped_delivery = _seed_delivery(
        skipped,
        status=DeliveryStatus.SENT,
        last_error=(f"{SKIPPED_LAST_ERROR_PREFIX}deadline_claim_status:UNDER_REVIEW"),
    )
    sms_delivery = _seed_delivery(sms_only, status=DeliveryStatus.SENT)
    sms_delivery.channel = NotificationChannel.SMS.value
    db_session.add_all(
        [pending_delivery, skipped_delivery, sms_delivery, _delivered(delivered)]
    )
    await db_session.flush()

    response = await client.get(
        "/api/v1/notifications",
        headers=await _session_tokens(db_session, api_clock, student),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [str(delivered.id)]

    # The gate bounds the unread filter identically: no hidden row
    # re-enters through ?unread=true.
    unread = await client.get(
        "/api/v1/notifications",
        params={"unread": "true"},
        headers=await _session_tokens(db_session, api_clock, student),
    )
    assert unread.json()["total"] == 1
    assert [item["id"] for item in unread.json()["items"]] == [str(delivered.id)]


async def test_teacher_and_anonymous_cannot_read_student_inbox(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: StepClock
) -> None:
    """The inbox is a Student capability surface (the identity guard):
    a TEACHER answers PERMISSION_DENIED 403 even with a live ACTIVE
    session; no bearer at all answers 401."""
    teacher = await _seed_user(
        db_session, username=f"tea-{uuid4().hex[:10]}", role=Role.TEACHER
    )
    teacher_headers = await _session_tokens(db_session, api_clock, teacher)

    denied = await client.get("/api/v1/notifications", headers=teacher_headers)
    assert denied.status_code == 403
    assert _envelope(denied)["code"] == "PERMISSION_DENIED"

    anonymous = await client.get("/api/v1/notifications")
    assert anonymous.status_code == 401


# --- mark-read (plan steps 3-4) -------------------------------------------------------


async def test_mark_read_is_idempotent(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: StepClock
) -> None:
    student = await _seed_user(db_session, username=f"stu-{uuid4().hex[:10]}")
    notification = _seed_notification(
        student,
        event_key=f"submission:{uuid4()}:approved",
        created_at=_T0 - timedelta(hours=1),
    )
    db_session.add(notification)
    await db_session.flush()
    headers = await _session_tokens(db_session, api_clock, student)

    first = await client.post(
        f"/api/v1/notifications/{notification.id}/read", headers=headers
    )
    assert first.status_code == 200
    body = first.json()
    assert set(body) == _ITEM_FIELDS
    # Pydantic JSON renders UTC as "Z"; parse back before comparing.
    assert datetime.fromisoformat(body["read_at"].replace("Z", "+00:00")) == _T0

    # Five minutes later the re-read is a no-op: the FIRST read_at
    # survives (a frozen clock could not distinguish this from a
    # re-stamp, hence the step clock).
    api_clock.advance(timedelta(minutes=5))
    second = await client.post(
        f"/api/v1/notifications/{notification.id}/read", headers=headers
    )
    assert second.status_code == 200
    assert (
        datetime.fromisoformat(second.json()["read_at"].replace("Z", "+00:00")) == _T0
    )

    await db_session.refresh(notification)
    assert notification.read_at == _T0


async def test_mark_read_rejects_foreign_and_missing_ids(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: StepClock
) -> None:
    """Own-only rows: another student's notification answers
    PERMISSION_DENIED and stays unread; an unknown id answers NOT_FOUND
    without leaking whether it exists."""
    owner = await _seed_user(db_session, username=f"stu-{uuid4().hex[:10]}")
    intruder = await _seed_user(db_session, username=f"stu-{uuid4().hex[:10]}")
    foreign = _seed_notification(
        owner,
        event_key=f"submission:{uuid4()}:approved",
        created_at=_T0 - timedelta(hours=1),
    )
    db_session.add(foreign)
    await db_session.flush()
    intruder_headers = await _session_tokens(db_session, api_clock, intruder)

    denied = await client.post(
        f"/api/v1/notifications/{foreign.id}/read", headers=intruder_headers
    )
    assert denied.status_code == 403
    assert _envelope(denied)["code"] == "PERMISSION_DENIED"
    await db_session.refresh(foreign)
    assert foreign.read_at is None

    missing = await client.post(
        f"/api/v1/notifications/{uuid4()}/read", headers=intruder_headers
    )
    assert missing.status_code == 404
    assert _envelope(missing)["code"] == "NOT_FOUND"


# --- admin failure surface (spec 25.4; Admin-only until scoped delegation) ------------


async def test_admin_failure_surface_lists_failed_deliveries(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: StepClock
) -> None:
    """FAILED deliveries with last_error + attempts, newest failure
    first; SENT/PENDING rows are absent; the Admin-only guard rejects a
    student, a CONFIRMED TOTP teacher (the PR #2 hardening flip), and a
    TOTP-unconfirmed admin (the setup-forcing error, not the role
    error)."""
    student = await _seed_user(db_session, username=f"stu-{uuid4().hex[:10]}")
    failed_old = _seed_notification(
        student,
        event_key=f"submission:{uuid4()}:approved",
        created_at=_T0 - timedelta(hours=2),
    )
    failed_new = _seed_notification(
        student,
        event_key=f"submission:{uuid4()}:approved",
        created_at=_T0 - timedelta(hours=1),
    )
    sent_one = _seed_notification(
        student,
        event_key=f"submission:{uuid4()}:approved",
        created_at=_T0 - timedelta(minutes=30),
    )
    db_session.add_all([failed_old, failed_new, sent_one])
    await db_session.flush()
    deliveries = [
        _seed_delivery(
            failed_old,
            status=DeliveryStatus.FAILED,
            attempts=3,
            updated_at=_T0 - timedelta(hours=2),
            last_error="temporary: provider unavailable",
        ),
        _seed_delivery(
            failed_new,
            status=DeliveryStatus.FAILED,
            attempts=1,
            updated_at=_T0 - timedelta(minutes=10),
            last_error="permanent: recipient blacklisted",
        ),
        _seed_delivery(
            sent_one,
            status=DeliveryStatus.SENT,
            attempts=1,
            updated_at=_T0 - timedelta(minutes=5),
        ),
    ]
    db_session.add_all(deliveries)
    await db_session.flush()

    _, admin_headers = await _seed_management_account(
        db_session, api_clock, username=f"adm-{uuid4().hex[:10]}"
    )
    response = await client.get(
        "/api/v1/admin/notification-failures", headers=admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == 2
    assert [item["id"] for item in body["items"]] == [
        str(deliveries[1].id),
        str(deliveries[0].id),
    ]
    newest_item = body["items"][0]
    assert set(newest_item) == {
        "id",
        "notification_id",
        "user_id",
        "event_key",
        "channel",
        "attempts",
        "last_error",
        "scheduled_at",
        "updated_at",
    }
    assert newest_item["attempts"] == 1
    assert newest_item["last_error"] == "permanent: recipient blacklisted"
    assert newest_item["channel"] == "IN_APP"
    assert newest_item["user_id"] == str(student.id)
    # The SENT delivery is operational state, not a failure.
    assert str(deliveries[2].id) not in {item["id"] for item in body["items"]}

    student_headers = await _session_tokens(db_session, api_clock, student)
    denied = await client.get(
        "/api/v1/admin/notification-failures", headers=student_headers
    )
    assert denied.status_code == 403
    assert _envelope(denied)["code"] == "PERMISSION_DENIED"

    # The hardening flip: an ACTIVE teacher WITH a confirmed TOTP
    # credential is still refused the failure surface — Admin-only
    # until scoped delegation (PR #2 hardening ruling).
    _, teacher_headers = await _seed_management_account(
        db_session, api_clock, username=f"tea-{uuid4().hex[:10]}", role=Role.TEACHER
    )
    teacher_denied = await client.get(
        "/api/v1/admin/notification-failures", headers=teacher_headers
    )
    assert teacher_denied.status_code == 403
    assert _envelope(teacher_denied)["code"] == "PERMISSION_DENIED"

    # The admin guard's own contract: an invited admin before TOTP
    # confirmation gets the setup-required code, not the role
    # permission code (capability-then-state precedence).
    _, unconfirmed_headers = await _seed_management_account(
        db_session, api_clock, username=f"adm-{uuid4().hex[:10]}", confirmed=False
    )
    unconfirmed = await client.get(
        "/api/v1/admin/notification-failures", headers=unconfirmed_headers
    )
    assert unconfirmed.status_code == 403
    assert _envelope(unconfirmed)["code"] == "TOTP_SETUP_REQUIRED"
