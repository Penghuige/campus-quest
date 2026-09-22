# backend/tests/integration/community/test_anonymous_moderation.py
"""Anonymous moderation boundaries and the Admin identity reveal (spec
§21.4; plan 06 task 8).

Three walls, in order of concern:

- **Teacher-safe moderation records.** The moderation surface (the task-6
  report queue renders comments in the ``ModerationComment`` shape) may
  correlate an anonymous author's comments within a Task through a
  pseudonymous ``moderation_key`` — stable per (task, author), keyed so
  it is neither reversible nor guessable, and never equal to (or derived
  from) the student number — but it must not carry the author's student
  number/login username, phone, email, nickname, or raw user id.
  Non-anonymous records show the nickname (already public) and NO key:
  deriving the key there would link an author's named and anonymous
  comments in one Task, which is exactly the §21.4 boundary the key
  exists to hold.
- **Admin reveal is an explicit, audited operation** (spec §21.4 每次追溯
  必须写 AuditLog): reason mandatory (typed rejection), Admin-only, and
  EVERY call — including repeats — emits a COMMENT_IDENTITY_REVEALED
  audit event carrying actor, comment id, reason, and timestamp through
  the injected publisher port (plan 08's AuditLog consumes the stream).
  The reveal response carries the real identity (user id, nickname,
  username == the student number) TO THE ADMIN ONLY; the ordinary
  moderation LIST never auto-reveals, not even for Admin viewers.
- **Self-report fold (task 6 review F2):** when the reporter IS the
  comment's author, the queue suppresses the reporter fields — the
  anonymous author's own nickname must not render alongside their
  anonymous comment. A separate reporter's identity still shows.

Harness: the ordinary rollback suite (no concurrency, no clocks beyond
the injected FrozenClock), so every seed lives in the savepoint-wrapped
``db_session``.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.error_codes import ErrorCode
from app.modules.audit.models import AuditLog
from app.modules.community.comment_service import CommentService
from app.modules.community.gates import CommentNotFoundError
from app.modules.community.models import Comment
from app.modules.community.moderation_service import (
    COMMENT_IDENTITY_REVEALED,
    COMMUNITY_IDENTITY_REVEAL,
    ModerationService,
    RevealDeniedError,
    RevealReasonRequiredError,
)
from app.modules.community.report_service import ReportService
from app.modules.community.schemas import ModerationComment, RevealedIdentity
from app.modules.community.serializers import derive_moderation_key
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor, InMemoryEventCollector
from app.modules.identity.models import User
from app.modules.tasks.enums import (
    DeadlineMode,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.models import Task

# Direct-insert password stub (argon2 hash of an unguessable test secret);
# the registration service is deliberately not exercised here.
_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)

_NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
_REVEAL_AT = _NOW + timedelta(hours=1)

# Fixed key material so every assertion is deterministic; production
# resolves the secret from Settings at the composition root.
_KEY_SECRET = "integration-moderation-key-secret"


# --- seeding helpers -------------------------------------------------------------


def _user(
    *,
    username: str,
    role: Role = Role.STUDENT,
    nickname: str = "测试同学",
    phone_e164: str | None = None,
    email: str | None = None,
) -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname=nickname,
        phone_e164=phone_e164,
        email_normalized=email,
        role=role,
        status=UserStatus.ACTIVE,
    )


def _task(owner: User, **overrides: Any) -> Task:
    fields: dict[str, Any] = {
        "owner_teacher_id": owner.id,
        "title": "校园歌手大赛观众报名数据整理",
        "description": "整理报名表的院系与曲目字段。",
        "task_type": TaskType.DATA_CRAWL,
        "rarity": TaskRarity.NORMAL,
        "base_reward_points": 100,
        "status": TaskStatus.PUBLISHED,
        "deadline_mode": DeadlineMode.RELATIVE,
        "duration_minutes": 4320,
        "allowed_file_types": ["CSV"],
        "max_file_size_bytes": 200 * 1024 * 1024,
        "notification_channels": ["SMS"],
    }
    fields.update(overrides)
    return Task(**fields)


def _comment(task: Task, user: User, **overrides: Any) -> Comment:
    fields: dict[str, Any] = {
        "task_id": task.id,
        "user_id": user.id,
        "content": "这个任务的说明很清楚，做起来很顺利。",
        "is_anonymous": True,
        "created_at": _NOW,
    }
    fields.update(overrides)
    return Comment(**fields)


async def _persist(session: AsyncSession, *objects: Any) -> None:
    """Add and flush; parents must be flushed before children reference
    their server-generated ids at construction time."""
    session.add_all(objects)
    await session.flush()


async def _fixture(
    db: AsyncSession,
) -> tuple[Task, User, User, User, User, User]:
    """A teacher-owned PUBLISHED task plus the identities the anonymity
    contract spans: the owner Teacher, the anonymous author (whose full
    identity facts the leakage assertions must never see), a second
    anonymous author, a separate reporter, and an Admin."""
    teacher = _user(username="teacher0001@pku.edu.cn", role=Role.TEACHER)
    author = _user(
        username="20250010001",
        nickname="发帖人甲",
        phone_e164="+8613800138000",
        email="author@pku.edu.cn",
    )
    second_author = _user(username="20250010002", nickname="跟帖人乙")
    reporter = _user(username="20250010003", nickname="举报人小报")
    admin = _user(username="admin-0001@pku.edu.cn", role=Role.ADMIN)
    await _persist(db, teacher, author, second_author, reporter, admin)
    task = _task(teacher)
    await _persist(db, task)
    return task, teacher, author, second_author, reporter, admin


async def _root_comment(
    db: AsyncSession, task: Task, user: User, **overrides: Any
) -> Comment:
    comment = _comment(task, user, **overrides)
    await _persist(db, comment)
    return comment


def _actor(user: User, *, role: Role | None = None) -> Actor:
    return Actor(user_id=user.id, role=role if role is not None else Role(user.role))


def _report_service() -> ReportService:
    return ReportService(moderation_key_secret=_KEY_SECRET)


def _moderation_service(
    *, events: InMemoryEventCollector | None = None
) -> tuple[ModerationService, InMemoryEventCollector]:
    collector = events if events is not None else InMemoryEventCollector()
    return (
        ModerationService(clock=FrozenClock(_REVEAL_AT), events=collector),
        collector,
    )


def _identity_facts(user: User) -> list[str]:
    """Every identity fact the moderation surface must never render for
    an anonymous author: student number / login username, nickname,
    phone, email, and the raw user id."""
    return [
        fact
        for fact in (
            user.username,
            user.nickname,
            user.phone_e164,
            user.email_normalized,
            str(user.id),
        )
        if fact is not None
    ]


async def _reported_queue_page(
    db: AsyncSession,
    actor: Actor,
    task: Task,
    comment: Comment,
    reporter: User,
    *,
    category: str = "SPAM",
) -> list[Any]:
    """File one report and hand back the actor's moderation queue page —
    the assembled surface the teacher-safety assertions read."""
    await _report_service().report_comment(db, reporter.id, comment.id, category)
    page, _total = await _report_service().list_task_reports(
        db, actor, task.id, limit=20, offset=0
    )
    return page


# --- teacher-safe moderation records (spec §21.4) ------------------------------------


@pytest.mark.integration
async def test_anonymous_moderation_record_carries_no_identity_facts(
    db_session: AsyncSession,
) -> None:
    """The Teacher moderation context shows 匿名用户 plus a pseudonymous
    key — never the anonymous author's student number (login username),
    phone, email, nickname, or raw user id, not even in the serialized
    payload."""
    task, teacher, author, _, reporter, _admin = await _fixture(db_session)
    comment = await _root_comment(db_session, task, author)

    page = await _reported_queue_page(
        db_session, _actor(teacher), task, comment, reporter
    )
    record = page[0].comment
    assert record.author_display == "匿名用户"
    assert record.moderation_key is not None
    assert record.moderation_key != author.username
    assert author.username not in record.moderation_key

    payload = json.dumps([dataclasses.asdict(item) for item in page], default=str)
    for fact in _identity_facts(author):
        assert fact not in payload


@pytest.mark.integration
def test_moderation_dto_has_no_identity_fields_by_construction() -> None:
    """The shape refuses author identity: no user_id, username, phone, or
    email field exists on ``ModerationComment`` — the display and the
    pseudonymous key are the only author-adjacent surfaces, and the
    reveal is never a field here."""
    fields = {field.name for field in dataclasses.fields(ModerationComment)}
    assert fields == {
        "id",
        "task_id",
        "parent_id",
        "content",
        "is_anonymous",
        "author_display",
        "created_at",
        "updated_at",
        "edited",
        "deleted",
        "moderation_key",
        "hard_hidden",
    }
    assert not any(name in fields for name in ("user_id", "username", "phone", "email"))


@pytest.mark.integration
async def test_moderation_key_stable_within_task_and_bound_to_secret(
    db_session: AsyncSession,
) -> None:
    """The key is the derived HMAC: the queue's value equals
    ``derive_moderation_key(task_id, user_id, secret=...)`` — 16 hex
    chars — and a different secret derives a different key (keyed, not a
    plain hash of the pair)."""
    task, teacher, author, _, reporter, _admin = await _fixture(db_session)
    comment = await _root_comment(db_session, task, author)

    page = await _reported_queue_page(
        db_session, _actor(teacher), task, comment, reporter
    )
    key = page[0].comment.moderation_key
    assert key == derive_moderation_key(task.id, author.id, secret=_KEY_SECRET)
    assert len(key) == 16
    assert all(char in "0123456789abcdef" for char in key)
    assert derive_moderation_key(
        task.id, author.id, secret="a-different-secret"
    ) != derive_moderation_key(task.id, author.id, secret=_KEY_SECRET)


@pytest.mark.integration
async def test_moderation_key_correlates_within_task_and_separates_authors(
    db_session: AsyncSession,
) -> None:
    """Same author + same task -> same key across their comments (thread
    correlation); a different anonymous author in the same task gets a
    different key (per-author separation)."""
    task, teacher, author, second_author, reporter, _admin = await _fixture(db_session)
    first = await _root_comment(db_session, task, author)
    second = await _root_comment(db_session, task, author, content="第二条匿名评论")
    others = await _root_comment(db_session, task, second_author)
    for comment, category in (
        (first, "SPAM"),
        (second, "OTHER"),
        (others, "HARASSMENT"),
    ):
        await _report_service().report_comment(
            db_session, reporter.id, comment.id, category
        )

    page, _total = await _report_service().list_task_reports(
        db_session, _actor(teacher), task.id, limit=20, offset=0
    )
    keys = {item.comment.id: item.comment.moderation_key for item in page}
    assert keys[first.id] == keys[second.id]
    assert keys[others.id] not in (keys[first.id], keys[second.id])
    assert len(set(keys.values())) == 2


@pytest.mark.integration
async def test_moderation_key_differs_across_tasks(
    db_session: AsyncSession,
) -> None:
    """The key is scoped per (task, author): the same author commenting
    anonymously on two tasks gets two unrelated keys, so correlation
    never crosses Task boundaries."""
    task, teacher, author, _, reporter, _admin = await _fixture(db_session)
    second_task = _task(teacher, title="第二个任务")
    await _persist(db_session, second_task)
    here = await _root_comment(db_session, task, author)
    there = await _root_comment(db_session, second_task, author)

    here_page = await _reported_queue_page(
        db_session, _actor(teacher), task, here, reporter
    )
    there_page = await _reported_queue_page(
        db_session, _actor(teacher), second_task, there, reporter, category="OTHER"
    )
    assert here_page[0].comment.moderation_key != there_page[0].comment.moderation_key


@pytest.mark.integration
async def test_named_moderation_record_shows_nickname_without_key(
    db_session: AsyncSession,
) -> None:
    """A non-anonymous record renders the (already public) nickname and
    NO moderation key: deriving the key there would join the author's
    named and anonymous comments in one Task — the deanonymization the
    key must not perform."""
    task, teacher, author, _, reporter, _admin = await _fixture(db_session)
    comment = await _root_comment(db_session, task, author, is_anonymous=False)

    page = await _reported_queue_page(
        db_session, _actor(teacher), task, comment, reporter
    )
    record = page[0].comment
    assert record.author_display == author.nickname
    assert record.moderation_key is None


@pytest.mark.integration
async def test_hard_hidden_comment_keeps_moderation_shape_with_flag(
    db_session: AsyncSession,
) -> None:
    """The task-3 shape carries: a hard-hidden anonymous comment still
    serializes for moderation with content intact, the hard_hidden flag
    set, the anonymous display, and the derived key."""
    task, teacher, author, _, reporter, admin = await _fixture(db_session)
    comment = await _root_comment(db_session, task, author)
    # The report predates the removal: anchored writes (reports) are
    # single-shot on live comments, and the queue reviews history after.
    await _report_service().report_comment(db_session, reporter.id, comment.id, "SPAM")
    await CommentService().admin_hard_hide_subtree(
        db_session, _actor(admin), comment.id, "涉及个人隐私"
    )

    page, _total = await _report_service().list_task_reports(
        db_session, _actor(teacher), task.id, limit=20, offset=0
    )
    record = page[0].comment
    assert record.hard_hidden is True
    assert record.deleted is True
    assert record.content is not None
    assert record.author_display == "匿名用户"
    assert record.moderation_key == derive_moderation_key(
        task.id, author.id, secret=_KEY_SECRET
    )


# --- the Admin reveal (spec §21.4 每次追溯必须写 AuditLog) ---------------------------


@pytest.mark.integration
async def test_reveal_requires_a_reason(db_session: AsyncSession) -> None:
    """Missing reason — None, empty, or whitespace-only — is the typed
    VALIDATION_ERROR, and no audit event is emitted for a rejected
    attempt."""
    task, _teacher, author, _, _, admin = await _fixture(db_session)
    comment = await _root_comment(db_session, task, author)
    service, events = _moderation_service()

    for bad_reason in (None, "", "   "):
        with pytest.raises(RevealReasonRequiredError) as raised:
            await service.request_identity_reveal(
                db_session, _actor(admin), comment.id, bad_reason
            )
        assert raised.value.code == ErrorCode.VALIDATION_ERROR
        assert raised.value.status_code == 400
    assert events.events == []


@pytest.mark.integration
async def test_reveal_is_admin_only(db_session: AsyncSession) -> None:
    """Teacher (even the task's owner) and Student are the typed
    PERMISSION_DENIED wall — the reveal is the one Admin-only community
    trace, and nothing is audited for the refused call."""
    task, teacher, author, _, _, admin = await _fixture(db_session)
    comment = await _root_comment(db_session, task, author)
    service, events = _moderation_service()

    for denied_actor in (_actor(teacher), _actor(author)):
        with pytest.raises(RevealDeniedError) as raised:
            await service.request_identity_reveal(
                db_session, denied_actor, comment.id, "接到投诉需要核实身份"
            )
        assert raised.value.code == ErrorCode.PERMISSION_DENIED
        assert raised.value.status_code == 403
    assert events.events == []


@pytest.mark.integration
async def test_reveal_unknown_comment_is_not_found(db_session: AsyncSession) -> None:
    task, _teacher, _, _, _, admin = await _fixture(db_session)
    service, events = _moderation_service()

    with pytest.raises(CommentNotFoundError) as raised:
        await service.request_identity_reveal(
            db_session, _actor(admin), uuid4(), "需要追溯"
        )
    assert raised.value.status_code == 404
    assert events.events == []


@pytest.mark.integration
async def test_valid_reveal_returns_identity_and_emits_audit_event(
    db_session: AsyncSession,
) -> None:
    """The explicit-reveal surface: an Admin with a reason gets the real
    identity (user id, nickname, username == the student number), and
    exactly one AUDIT-grade event lands on the publisher port carrying
    actor, comment id, reason, and the frozen timestamp. The comment row
    itself is untouched — the reveal reads, it never marks."""
    task, _teacher, author, _, _, admin = await _fixture(db_session)
    comment = await _root_comment(db_session, task, author)
    service, events = _moderation_service()

    revealed = await service.request_identity_reveal(
        db_session, _actor(admin), comment.id, "  接到实名投诉，需核实发帖人  "
    )

    assert isinstance(revealed, RevealedIdentity)
    assert revealed.user_id == author.id
    assert revealed.nickname == author.nickname
    assert revealed.username == author.username  # the student number

    revealed_events = events.of_type(COMMENT_IDENTITY_REVEALED)
    assert len(revealed_events) == 1
    event = revealed_events[0]
    assert event.aggregate_type == "Comment"
    assert event.aggregate_id == comment.id
    assert event.occurred_at == _REVEAL_AT
    assert event.payload["actor_user_id"] == str(admin.id)
    assert event.payload["comment_id"] == str(comment.id)
    assert event.payload["reason"] == "接到实名投诉，需核实发帖人"  # trimmed
    assert event.payload["revealed_user_id"] == str(author.id)

    await db_session.refresh(comment)
    assert comment.deleted_at is None
    assert comment.is_hard_hidden is False
    assert comment.content == "这个任务的说明很清楚，做起来很顺利。"


@pytest.mark.integration
async def test_valid_reveal_writes_a_durable_audit_row(
    db_session: AsyncSession,
) -> None:
    """G12 durable audit (PR #2 hardening P0-5): every accepted reveal
    commits exactly one ``audit_logs`` row in the SAME transaction as
    the call — actor (id + role snapshot), the COMMUNITY_IDENTITY_REVEAL
    action, the comment target, the TRIMMED reason, the database
    timestamp, and what was disclosed (``revealed_user_id``) in details
    — beside the unchanged DomainEvent stream (V1 direct-write ruling:
    no event-consumer pipeline)."""
    task, _teacher, author, _, _, admin = await _fixture(db_session)
    comment = await _root_comment(db_session, task, author)
    service, events = _moderation_service()

    revealed = await service.request_identity_reveal(
        db_session, _actor(admin), comment.id, "  治理复核：第三次实名投诉  "
    )
    assert revealed.user_id == author.id

    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.target_id == str(comment.id))
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.action == COMMUNITY_IDENTITY_REVEAL
    assert row.actor_user_id == admin.id
    assert row.actor_role == Role.ADMIN.value
    assert row.target_type == "comment"
    assert row.target_id == str(comment.id)
    assert row.reason == "治理复核：第三次实名投诉"  # trimmed
    assert row.details == {
        "task_id": str(task.id),
        "revealed_user_id": str(author.id),
    }
    assert row.created_at is not None
    # The durable row and the DomainEvent are one trace, one call.
    assert len(events.of_type(COMMENT_IDENTITY_REVEALED)) == 1


@pytest.mark.integration
async def test_repeated_reveal_emits_an_event_every_time(
    db_session: AsyncSession,
) -> None:
    """每次追溯 MUST AuditLog: re-revealing the same comment is allowed
    and each call emits its own event with its own reason — the audit
    trail records every look, not every comment."""
    task, _teacher, author, _, _, admin = await _fixture(db_session)
    comment = await _root_comment(db_session, task, author)
    service, events = _moderation_service()

    first = await service.request_identity_reveal(
        db_session, _actor(admin), comment.id, "第一次核实"
    )
    second = await service.request_identity_reveal(
        db_session, _actor(admin), comment.id, "二次复核"
    )

    assert (first.user_id, first.username) == (second.user_id, second.username)
    revealed_events = events.of_type(COMMENT_IDENTITY_REVEALED)
    assert len(revealed_events) == 2
    assert [event.payload["reason"] for event in revealed_events] == [
        "第一次核实",
        "二次复核",
    ]
    # 每次追溯 in the DURABLE trail too: two audit rows, one per call.
    audit_rows = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.target_id == str(comment.id))
            )
        )
        .scalars()
        .all()
    )
    assert [row.reason for row in audit_rows] == ["第一次核实", "二次复核"]


@pytest.mark.integration
async def test_reveal_reaches_removed_comments(db_session: AsyncSession) -> None:
    """Governance reaches history: a hard-hidden (privacy/legal removed)
    comment is still revealable — that removal is precisely the flow the
    audited trace exists for."""
    task, _teacher, author, _, _, admin = await _fixture(db_session)
    comment = await _root_comment(db_session, task, author)
    await CommentService().admin_hard_hide_subtree(
        db_session, _actor(admin), comment.id, "隐私下架"
    )
    service, events = _moderation_service()

    revealed = await service.request_identity_reveal(
        db_session, _actor(admin), comment.id, "隐私下架后的实名追溯"
    )

    assert revealed.user_id == author.id
    assert len(events.of_type(COMMENT_IDENTITY_REVEALED)) == 1


# --- the ordinary moderation list never auto-reveals (spec §21.4) --------------------


@pytest.mark.integration
async def test_admin_queue_listing_stays_anonymous_until_explicit_reveal(
    db_session: AsyncSession,
) -> None:
    """Admin viewing the queue changes nothing: the anonymous record
    stays 匿名用户 + key, the serialized page carries none of the
    author's identity facts, and the identity only appears through the
    separate explicit reveal call."""
    task, _teacher, author, _, reporter, admin = await _fixture(db_session)
    comment = await _root_comment(db_session, task, author)

    page = await _reported_queue_page(
        db_session, _actor(admin), task, comment, reporter
    )
    record = page[0].comment
    assert record.author_display == "匿名用户"
    assert record.moderation_key == derive_moderation_key(
        task.id, author.id, secret=_KEY_SECRET
    )
    payload = json.dumps([dataclasses.asdict(item) for item in page], default=str)
    for fact in _identity_facts(author):
        assert fact not in payload

    service, events = _moderation_service()
    revealed = await service.request_identity_reveal(
        db_session, _actor(admin), comment.id, "治理需要实名核实"
    )
    assert revealed.user_id == author.id
    assert len(events.of_type(COMMENT_IDENTITY_REVEALED)) == 1


# --- self-report fold (task 6 review F2) ---------------------------------------------


@pytest.mark.integration
async def test_self_report_suppresses_reporter_fields(db_session: AsyncSession) -> None:
    """When the reporter IS the comment's author, the queue renders no
    reporter identity — the anonymous author's own nickname must not
    surface next to their anonymous comment."""
    task, teacher, author, _, _, _admin = await _fixture(db_session)
    comment = await _root_comment(db_session, task, author)

    page = await _reported_queue_page(
        db_session, _actor(teacher), task, comment, author
    )
    view = page[0]
    assert view.reporter_user_id is None
    assert view.reporter_nickname is None
    assert view.comment.author_display == "匿名用户"
    payload = json.dumps([dataclasses.asdict(item) for item in page], default=str)
    for fact in _identity_facts(author):
        assert fact not in payload


@pytest.mark.integration
async def test_separate_reporter_fields_survive_the_fold(
    db_session: AsyncSession,
) -> None:
    """The fold is exactly the self-report edge: a different reporter's
    identity still renders for the moderators who act on the report."""
    task, teacher, author, _, reporter, _admin = await _fixture(db_session)
    comment = await _root_comment(db_session, task, author)

    page = await _reported_queue_page(
        db_session, _actor(teacher), task, comment, reporter
    )
    view = page[0]
    assert view.reporter_user_id == reporter.id
    assert view.reporter_nickname == reporter.nickname
